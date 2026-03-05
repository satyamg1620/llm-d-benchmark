"""
Step 06: Standalone vLLM Deployment

Deploys vLLM models as standalone Kubernetes Deployments and Services.
This is the simpler deployment path (vs modelservice) where each model
gets a direct Deployment + Service without the GAIE routing layer.

Includes:
- Deployment + Service application
- Optional PodMonitor for Prometheus scraping
- Optional HTTPRoute for Gateway API routing
- OpenShift route creation with --target-port
- Log collection after pods are ready
- Standup parameter propagation via ConfigMap

Per-stack step: runs once per rendered stack directory.
"""

from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class StandaloneDeployStep(Step):
    """Deploy vLLM models as standalone Kubernetes Deployments and Services."""

    def __init__(self):
        super().__init__(
            number=6,
            name="standalone_deploy",
            description="Deploy vLLM standalone models (Deployment + Service)",
            phase=Phase.STANDUP,
            per_stack=True,
        )

    def should_skip(self, context: ExecutionContext) -> bool:
        # Only run for standalone deployments
        return "standalone" not in context.deployed_methods

    def execute(  # pylint: disable=too-many-branches,too-many-locals
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

        # Load stack config for model info
        plan_config = self._load_stack_config(stack_path)
        namespace = context.namespace or "default"

        # Find standalone deployment YAML
        deploy_yaml = self._find_yaml(stack_path, "14_standalone-deployment")
        service_yaml = self._find_yaml(stack_path, "15_standalone-service")

        if not deploy_yaml:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=True,
                message="No standalone deployment YAML found, skipping",
                stack_name=stack_path.name,
            )

        # Apply the deployment
        result = cmd.kubectl("apply", "-f", str(deploy_yaml))
        if not result.success:
            errors.append(
                f"Failed to apply standalone deployment: {result.stderr}"
            )

        # Apply the service
        if service_yaml:
            result = cmd.kubectl("apply", "-f", str(service_yaml))
            if not result.success:
                errors.append(
                    f"Failed to apply standalone service: {result.stderr}"
                )

        # Apply PodMonitor for Prometheus scraping (if rendered)
        podmonitor_yaml = self._find_yaml(stack_path, "17_standalone-podmonitor")
        if podmonitor_yaml:
            result = cmd.kubectl("apply", "-f", str(podmonitor_yaml))
            if not result.success:
                context.logger.log_warning(
                    f"PodMonitor apply failed (non-fatal): {result.stderr}"
                )
            else:
                context.logger.log_info(
                    "📊 PodMonitor created for Prometheus scraping"
                )

        # Apply HTTPRoute (if rendered and enabled)
        httproute_yaml = self._find_yaml(stack_path, "08_httproute")
        if httproute_yaml:
            result = cmd.kubectl("apply", "-f", str(httproute_yaml))
            if not result.success:
                context.logger.log_warning(
                    f"HTTPRoute apply failed (non-fatal): {result.stderr}"
                )
            else:
                context.logger.log_info("🔀 HTTPRoute created")

        # Extract deployment name from YAML (file exists from plan phase)
        deploy_name = None
        try:
            with open(deploy_yaml, encoding="utf-8") as f:
                deploy_config = yaml.safe_load(f)
            deploy_name = deploy_config.get("metadata", {}).get(
                "name", ""
            )
        except (yaml.YAMLError, OSError):
            pass

        # Wait for rollout (CommandExecutor handles dry-run logging)
        if deploy_name and not errors:
            wait_result = cmd.kubectl(
                "rollout", "status",
                f"deployment/{deploy_name}",
                "--namespace", namespace,
                "--timeout=900s",
            )
            if not wait_result.success:
                errors.append(
                    "Standalone deployment rollout failed: "
                    f"{wait_result.stderr}"
                )

        # Collect logs after pods are ready (non-fatal)
        if deploy_name and not errors and not context.dry_run:
            self._collect_logs(cmd, context, namespace, deploy_name)

        # Extract service endpoint for smoketest (YAML exists from plan phase)
        if service_yaml:
            try:
                with open(service_yaml, encoding="utf-8") as f:
                    svc_config = yaml.safe_load(f)
                svc_name = svc_config.get("metadata", {}).get(
                    "name", ""
                )
                if svc_name:
                    context.deployed_endpoints[stack_path.name] = (
                        svc_name
                    )
            except (yaml.YAMLError, OSError):
                pass

        # Create OpenShift route if applicable (with --target-port)
        if context.is_openshift and service_yaml:
            self._create_openshift_route(
                cmd, context, plan_config, service_yaml
            )

        # Log resource snapshot
        if not errors:
            resource_types = "deployment,service,pods,secrets"
            if context.is_openshift:
                resource_types += ",route"
            cmd.kubectl(
                "get", resource_types,
                "--namespace", namespace,
            )

        # Propagate standup parameters as ConfigMap
        self._propagate_standup_parameters(cmd, context, plan_config)

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Standalone deployment had errors",
                errors=errors,
                stack_name=stack_path.name,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=(
                f"Standalone deployment applied from {stack_path.name}"
            ),
            stack_name=stack_path.name,
        )

    def _collect_logs(
        self, cmd: CommandExecutor, context: ExecutionContext,
        namespace: str, deploy_name: str
    ):
        """Collect vLLM pod logs after deployment is ready."""
        logs_dir = context.setup_logs_dir()
        result = cmd.kubectl(
            "get", "pods",
            "-l", f"app={deploy_name}",
            "--namespace", namespace,
            "-o", "jsonpath={.items[*].metadata.name}",
        )
        if result.success and result.stdout.strip():
            pod_names = result.stdout.strip().split()
            for pod_name in pod_names:
                log_result = cmd.kubectl(
                    "logs", pod_name,
                    "--namespace", namespace,
                    "--tail=-1",
                )
                if log_result.success:
                    log_file = logs_dir / f"{pod_name}.log"
                    log_file.write_text(
                        log_result.stdout, encoding="utf-8"
                    )

    def _create_openshift_route(
        self,
        cmd: CommandExecutor,
        context: ExecutionContext,
        plan_config: dict,
        service_yaml: Path,
    ):
        """Expose a service as an OpenShift route with --target-port."""
        try:
            with open(service_yaml, encoding="utf-8") as f:
                svc_config = yaml.safe_load(f)
            svc_name = svc_config.get("metadata", {}).get("name", "")
            namespace = context.namespace or "default"

            # Get inference port for target-port
            inference_port = (
                plan_config.get("vllmCommon", {})
                .get("inferencePort", 8000)
            )

            if svc_name:
                route_name = f"{svc_name}-route"
                # Check if route already exists
                check = cmd.oc(
                    "get", "route", route_name,
                    "-n", namespace, "--ignore-not-found",
                )
                if check.success and not check.stdout.strip():
                    cmd.oc(
                        "expose", f"service/{svc_name}",
                        f"--name={route_name}",
                        f"--target-port={inference_port}",
                        "-n", namespace,
                    )
        except (yaml.YAMLError, OSError):
            pass

    def _propagate_standup_parameters(
        self, cmd: CommandExecutor, context: ExecutionContext,
        plan_config: dict
    ):
        """Save key standup parameters as ConfigMap ``llm-d-benchmark-standup-parameters``
        so run-phase pods can read deployment metadata."""
        harness_ns = context.harness_namespace or context.namespace or "default"
        cm_name = "llm-d-benchmark-standup-parameters"

        # Build a flat dict of key standup parameters
        params = {
            "namespace": context.namespace or "",
            "harness_namespace": harness_ns,
            "deploy_methods": ",".join(context.deployed_methods),
            "platform_type": context.platform_type,
            "cluster_name": context.cluster_name or "",
        }

        # Add relevant plan config values
        if plan_config:
            model_config = plan_config.get("model", {})
            params["model_name"] = model_config.get("name", "")
            params["model_short_name"] = model_config.get("shortName", "")
            vllm_config = plan_config.get("vllmCommon", {})
            params["inference_port"] = str(
                vllm_config.get("inferencePort", 8000)
            )

        # Build --from-literal args
        literal_args = []
        for key, value in params.items():
            literal_args.append(f"--from-literal={key}={value}")

        create_args = [
            "create", "configmap", cm_name,
            "--namespace", harness_ns,
        ] + literal_args + ["--dry-run=client", "-o", "yaml"]

        result = cmd.kubectl(*create_args)
        if result.success:
            yaml_path = (
                context.setup_yamls_dir() / "standup-parameters.yaml"
            )
            yaml_path.write_text(result.stdout, encoding="utf-8")
            cmd.kubectl("apply", "-f", str(yaml_path))
