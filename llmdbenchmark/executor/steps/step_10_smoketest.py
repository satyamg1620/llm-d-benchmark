"""
Step 10: Smoketest

Validates that the deployment was successful by:
1. Checking that pods are running and ready
2. Finding the service/gateway endpoint and IP
3. Testing pod IPs directly with curl
4. Testing the service/gateway endpoint
5. Optionally testing OpenShift routes

Handles both standalone (Service-based) and modelservice (Gateway-based)
deployments with proper IP resolution for each type.

Per-stack step: runs once per rendered stack directory.
"""

import json
from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class SmoketestStep(Step):
    """Validate deployment health and model serving via smoketests."""

    def __init__(self):
        super().__init__(
            number=10,
            name="smoketest",
            description="Validate deployment health and model serving",
            phase=Phase.STANDUP,
            per_stack=True,
        )

    def execute(  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
        self, context: ExecutionContext, stack_path: Path | None = None
    ) -> StepResult:
        if stack_path is None:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="No stack path provided for per-stack step",
                errors=["stack_path is required"],
            )

        errors = []
        cmd = CommandExecutor(
            work_dir=context.workspace,
            dry_run=context.dry_run,
            verbose=context.verbose,
            logger=context.logger,
            kubeconfig=context.kubeconfig,
            openshift=context.is_openshift,
        )

        namespace = context.namespace or "default"
        stack_name = stack_path.name
        is_standalone = "standalone" in context.deployed_methods

        # Load stack config to get model info
        plan_config = self._load_stack_config(stack_path)
        model_name = plan_config.get("model", {}).get("name", "")
        inference_port = plan_config.get("vllmCommon", {}).get(
            "inferencePort", 8000
        )
        release = plan_config.get("release", "llmdbench")

        # Determine the gateway port (80 default, 443 for HTTPS)
        gateway_port = "80"
        service_ip = None
        service_name = None

        if context.dry_run:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=True,
                message=(
                    f"[DRY RUN] Smoketest commands logged for {stack_name}"
                ),
                stack_name=stack_name,
            )

        # 1. Find service/gateway IP
        if is_standalone:
            service_ip, service_name, gateway_port = (
                self._find_standalone_endpoint(
                    cmd, namespace, inference_port
                )
            )
        else:
            service_ip, service_name, gateway_port = (
                self._find_gateway_endpoint(
                    cmd, namespace, release
                )
            )

        if not service_ip:
            errors.append(
                f"Could not find service/gateway IP for smoketest"
            )

        # 2. Check pods are running
        if is_standalone:
            pod_selector = "app=vllm-standalone"
        else:
            pod_selector = "llm-d.ai/role=decode"

        pod_check = cmd.kubectl(
            "get", "pods",
            "-l", pod_selector,
            "--namespace", namespace,
            "-o", "jsonpath={.items[*].status.phase}",
        )

        if pod_check.success:
            phases = pod_check.stdout.strip().split()
            if not phases:
                errors.append(
                    f"No pods found with selector '{pod_selector}'"
                )
            elif not all(p == "Running" for p in phases):
                errors.append(
                    "Not all pods running "
                    f"(found: {', '.join(phases)})"
                )
        else:
            errors.append(
                f"Failed to check pod status: {pod_check.stderr}"
            )

        # 3. Get pod IPs and test each pod
        pod_ips_result = cmd.kubectl(
            "get", "pods",
            "-l", pod_selector,
            "--namespace", namespace,
            "-o", "jsonpath={.items[*].status.podIP}",
        )

        if pod_ips_result.success and pod_ips_result.stdout.strip():
            pod_ips = pod_ips_result.stdout.strip().split()
            for pod_ip in pod_ips:
                test_result = self._test_endpoint(
                    cmd, namespace, pod_ip, inference_port,
                    model_name, plan_config,
                )
                if test_result:
                    errors.append(test_result)
        elif not errors:
            errors.append("No pod IPs found for smoketest")

        # 4. Test service/gateway endpoint
        if service_ip:
            context.logger.log_info(
                f"Testing service/gateway \"{service_ip}\" "
                f"(port {gateway_port})..."
            )
            test_result = self._test_endpoint(
                cmd, namespace, service_ip, gateway_port,
                model_name, plan_config,
            )
            if test_result:
                errors.append(
                    f"Service test failed: {test_result}"
                )

        # 5. Test OpenShift route if applicable
        if context.is_openshift:
            self._test_openshift_route(
                cmd, context, namespace, model_name, plan_config,
                gateway_port, errors,
            )

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Smoketest failed",
                errors=errors,
                stack_name=stack_name,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=f"All smoketests passed for {stack_name}",
            stack_name=stack_name,
        )

    def _find_standalone_endpoint(
        self, cmd: CommandExecutor, namespace: str, inference_port: int
    ) -> tuple[str | None, str | None, str]:
        """Find standalone service IP."""
        result = cmd.kubectl(
            "get", "service",
            "-l", "stood-up-from=llm-d-benchmark",
            "--namespace", namespace,
            "-o", "jsonpath={.items[0].spec.clusterIP}:{.items[0].metadata.name}",
        )
        if result.success and result.stdout.strip():
            parts = result.stdout.strip().split(":", 1)
            ip = parts[0] if parts else None
            name = parts[1] if len(parts) > 1 else None
            return ip, name, str(inference_port)
        return None, None, str(inference_port)

    def _find_gateway_endpoint(
        self, cmd: CommandExecutor, namespace: str, release: str
    ) -> tuple[str | None, str | None, str]:
        """Find the gateway IP from the Gateway custom resource.

        Extracts the IP from ``status.addresses`` and detects HTTPS by
        checking for ``https`` listener entries in ``managedFields``.
        """
        gateway_name = f"infra-{release}-inference-gateway"
        gateway_port = "80"

        # Try to get gateway custom object
        result = cmd.kubectl(
            "get", "gateway", gateway_name,
            "--namespace", namespace,
            "-o", "json",
        )
        if result.success and result.stdout.strip():
            try:
                gw_data = json.loads(result.stdout)

                # Check for HTTPS listeners
                managed_fields = gw_data.get("metadata", {}).get(
                    "managedFields", []
                )
                for mf in managed_fields:
                    fields_v1 = mf.get("fieldsV1", {})
                    f_status = fields_v1.get("f:status", {})
                    f_listeners = f_status.get("f:listeners", {})
                    for key in f_listeners:
                        if "https" in key.lower():
                            gateway_port = "443"
                            break

                # Extract IP from status.addresses
                addresses = gw_data.get("status", {}).get("addresses", [])
                for addr in addresses:
                    addr_type = addr.get("type", "")
                    value = addr.get("value", "")
                    if addr_type == "IPAddress" and value:
                        return value, gateway_name, gateway_port
                    if addr_type == "Hostname" and value:
                        return value, gateway_name, gateway_port

            except (json.JSONDecodeError, KeyError):
                pass

        # Fallback: try service lookup
        result = cmd.kubectl(
            "get", "service", gateway_name,
            "--namespace", namespace,
            "-o", "jsonpath={.spec.clusterIP}",
        )
        if result.success and result.stdout.strip():
            return result.stdout.strip(), gateway_name, gateway_port

        return None, gateway_name, gateway_port

    def _test_endpoint(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        cmd: CommandExecutor,
        namespace: str,
        host: str,
        port: str | int,
        expected_model: str,
        plan_config: dict | None = None,
    ) -> str | None:
        """Test an endpoint by querying /v1/models with retry.

        Returns an error string if the test fails, None on success.
        """
        # Build the curl command with retry
        protocol = "https" if str(port) == "443" else "http"

        # Build pull secret args if configured
        pull_secret_args = []
        if plan_config:
            pull_secret = (
                plan_config.get("vllmCommon", {})
                .get("imagePullSecrets", "")
            )
            if pull_secret:
                pull_secret_args = [
                    "--overrides",
                    json.dumps({
                        "spec": {
                            "imagePullSecrets": [{"name": pull_secret}]
                        }
                    }),
                ]

        # Use kubectl to curl from inside the cluster with retry
        curl_cmd = (
            f"curl -sk --retry 3 --retry-delay 5 --max-time 30 "
            f"{protocol}://{host}:{port}/v1/models"
        )

        kubectl_args = [
            "run", "smoketest-curl", "--rm", "-i", "--restart=Never",
            "--namespace", namespace,
            "--image=curlimages/curl",
        ] + pull_secret_args + [
            "--", "sh", "-c", curl_cmd,
        ]

        result = cmd.kubectl(*kubectl_args)

        if not result.success:
            return (
                f"Curl to {host}:{port} failed: "
                f"{result.stderr[:200]}"
            )

        # Parse JSON response and check for expected model
        if expected_model and result.stdout.strip():
            try:
                models_response = json.loads(result.stdout.strip())
                model_ids = [
                    m.get("id", "") for m in models_response.get("data", [])
                ]
                if expected_model not in model_ids:
                    return (
                        f"Endpoint {host}:{port} did not return expected "
                        f"model '{expected_model}'. "
                        f"Available models: {model_ids}"
                    )
            except (json.JSONDecodeError, KeyError, TypeError):
                # Fallback to string matching if JSON parsing fails
                if expected_model not in result.stdout:
                    return (
                        f"Endpoint {host}:{port} did not return expected "
                        f"model '{expected_model}'. "
                        f"Got: {result.stdout[:200]}"
                    )

        return None

    def _test_openshift_route(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        cmd: CommandExecutor,
        _context: ExecutionContext,
        namespace: str,
        model_name: str,
        plan_config: dict,
        gateway_port: str,
        errors: list,
    ):
        """Test the OpenShift route endpoint."""
        route_result = cmd.oc(
            "get", "route", "-n", namespace,
            "-o", "jsonpath={.items[0].spec.host}",
        )
        if route_result.success and route_result.stdout.strip():
            route_host = route_result.stdout.strip().strip("'")
            test_result = self._test_endpoint(
                cmd, namespace, route_host, gateway_port,
                model_name, plan_config,
            )
            if test_result:
                errors.append(f"Route test failed: {test_result}")
        else:
            _context.logger.log_warning(
                "Unable to fetch OpenShift route"
            )
