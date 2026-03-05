"""
Step 09: Deploy via ModelService

Deploys the model via the llm-d modelservice Helm chart, which creates
decode and prefill InferencePools with the full routing stack.

This step:
1. Applies the modelservice helmfile release
2. Creates HTTPRoute for traffic routing
3. Waits for decode, prefill, and inference pool pods
4. Creates PodMonitor for Prometheus scraping
5. Collects pod logs
6. Optionally installs WVA (Workload Variant Autoscaler)
7. Optionally creates OpenShift routes
8. Propagates standup parameters as ConfigMap
9. Manages OpenShift SCCs if needed

Per-stack step: runs once per rendered stack directory.
"""

import shutil
from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class DeployModelserviceStep(Step):
    """Deploy the model via the llm-d modelservice Helm chart."""

    def __init__(self):
        super().__init__(
            number=9,
            name="deploy_modelservice",
            description="Deploy model via modelservice Helm chart",
            phase=Phase.STANDUP,
            per_stack=True,
        )

    def should_skip(self, context: ExecutionContext) -> bool:
        return "modelservice" not in context.deployed_methods

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

        # Load stack config
        plan_config = self._load_stack_config(stack_path)
        release = plan_config.get("release", "llmdbench")
        model_short = plan_config.get("model", {}).get("shortName", stack_name)
        inference_port = (
            plan_config.get("vllmCommon", {}).get("inferencePort", 8000)
        )

        # 0. OpenShift SCC management — add anyuid/privileged if needed
        if context.is_openshift:
            self._manage_sccs(cmd, context, plan_config, namespace)

        # 1. Deploy modelservice via helmfile from helm working dir
        ms_values = self._find_yaml(stack_path, "13_ms-values")
        if not ms_values:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=True,
                message="No modelservice values found, skipping",
                stack_name=stack_name,
            )

        # Use helm working directory (prepared by step 07)
        helm_dir = context.setup_helm_dir() / stack_name
        helmfile_work = helm_dir / "helmfile.yaml"

        if helmfile_work.exists():
            result = cmd.helmfile(
                "--namespace", namespace,
                "--selector", f"name={model_short}-ms",
                "apply", "-f", str(helmfile_work),
                "--skip-diff-on-install", "--skip-schema-validation",
            )
            if not result.success:
                errors.append(
                    f"Failed to deploy modelservice: {result.stderr}"
                )
        else:
            # Fallback to original helmfile
            main_helmfile = self._find_yaml(stack_path, "10_helmfile-main")
            if main_helmfile:
                result = cmd.helmfile(
                    "--namespace", namespace,
                    "--selector", f"name={model_short}-ms",
                    "apply", "-f", str(main_helmfile),
                    "--skip-diff-on-install", "--skip-schema-validation",
                )
                if not result.success:
                    errors.append(
                        f"Failed to deploy modelservice: {result.stderr}"
                    )

        # 2. Apply HTTPRoute
        httproute_yaml = self._find_yaml(stack_path, "08_httproute")
        if httproute_yaml:
            result = cmd.kubectl("apply", "-f", str(httproute_yaml))
            if not result.success:
                errors.append(
                    f"Failed to apply HTTPRoute: {result.stderr}"
                )

        # 3. Wait for pods with live progress
        if not errors:
            # Wait for decode pods
            decode_wait = cmd.kubectl_wait_for_pods(
                label="llm-d.ai/role=decode",
                namespace=namespace,
                timeout=900,
                poll_interval=10,
                description="decode pods",
            )
            if not decode_wait.success:
                errors.append(
                    f"Decode pods not ready: {decode_wait.stderr}"
                )

            # Verify expected pod count for multinode deployments
            expected_replicas = int(
                plan_config.get("vllmCommon", {}).get("replicas", 1)
            )
            if expected_replicas > 1 and not context.dry_run:
                pod_count_result = cmd.kubectl(
                    "get", "pods",
                    "-l", "llm-d.ai/role=decode",
                    "--namespace", namespace,
                    "-o", "jsonpath={.items[*].metadata.name}",
                )
                if pod_count_result.success:
                    actual_count = len(
                        pod_count_result.stdout.strip().split()
                    ) if pod_count_result.stdout.strip() else 0
                    if actual_count < expected_replicas:
                        context.logger.log_warning(
                            f"⚠️  Expected {expected_replicas} decode pods "
                            f"but found {actual_count}"
                        )
                    else:
                        context.logger.log_info(
                            f"✅ Decode pod count: {actual_count}/{expected_replicas}"
                        )

            # Wait for prefill pods (optional — not all deployments have them)
            prefill_wait = cmd.kubectl_wait_for_pods(
                label="llm-d.ai/role=prefill",
                namespace=namespace,
                timeout=900,
                poll_interval=10,
                description="prefill pods",
            )
            if not prefill_wait.success:
                stderr_lower = prefill_wait.stderr.lower()
                if ("no matching resources found" not in stderr_lower
                        and "no pods found" not in stderr_lower):
                    errors.append(
                        f"Prefill pods not ready: {prefill_wait.stderr}"
                    )

            # Wait for inference pool (endpoint-picker)
            pool_wait = cmd.kubectl_wait_for_pods(
                label="app.kubernetes.io/component=endpoint-picker",
                namespace=namespace,
                timeout=300,
                poll_interval=10,
                description="inference pool",
            )
            if not pool_wait.success:
                stderr_lower = pool_wait.stderr.lower()
                if ("no matching resources found" not in stderr_lower
                        and "no pods found" not in stderr_lower):
                    errors.append(
                        f"Inference pool not ready: {pool_wait.stderr}"
                    )

        # 4. Collect pod logs (non-fatal)
        if not errors and not context.dry_run:
            self._collect_logs(cmd, context, namespace)

        # 5. Apply PodMonitor for Prometheus scraping (if rendered)
        podmonitor_yaml = self._find_yaml(stack_path, "17_podmonitor")
        if not podmonitor_yaml:
            podmonitor_yaml = self._find_yaml(stack_path, "18_podmonitor")
        if podmonitor_yaml:
            result = cmd.kubectl("apply", "-f", str(podmonitor_yaml))
            if not result.success:
                context.logger.log_warning(
                    f"PodMonitor apply failed (non-fatal): {result.stderr}"
                )
            else:
                context.logger.log_info(
                    "PodMonitor created for Prometheus scraping"
                )

        # 6. Record deployed endpoint
        gateway_class = plan_config.get("gateway", {}).get(
            "className", "istio"
        )

        if gateway_class == "kgateway":
            service_name = f"infra-{release}-inference-gateway"
        else:
            service_name = f"{model_short}-gaie-epp"

        context.deployed_endpoints[stack_name] = service_name

        # 7. Label the gateway with username and deployment method
        username = context.username or "unknown"
        cmd.kubectl(
            "label",
            f"gateway/infra-{release}-inference-gateway",
            f"stood-up-by={username}",
            "stood-up-from=llm-d-benchmark",
            "stood-up-via=modelservice",
            "--namespace", namespace,
            "--overwrite",
        )

        # 8. Create OpenShift route if applicable (with --target-port)
        if context.is_openshift and service_name:
            route_name = f"{release}-inference-gateway-route"
            check = cmd.oc(
                "get", "route", route_name,
                "-n", namespace, "--ignore-not-found",
            )
            if check.success and not check.stdout.strip():
                cmd.oc(
                    "expose", f"service/{service_name}",
                    f"--name={route_name}",
                    f"--target-port={inference_port}",
                    "-n", namespace,
                )

        # 9. Install WVA if enabled
        wva_config = plan_config.get("wva", {})
        if wva_config.get("enabled", False):
            self._install_wva(cmd, context, plan_config, errors)

        # 10. Propagate standup parameters
        self._propagate_standup_parameters(cmd, context, plan_config)

        # 11. Log resource snapshot
        if not errors:
            resource_types = "deployment,service,pods,gateway,httproute"
            if context.is_openshift:
                resource_types += ",route"
            cmd.kubectl(
                "get", resource_types,
                "--namespace", namespace,
            )

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Modelservice deployment had errors",
                errors=errors,
                stack_name=stack_name,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=f"Modelservice deployed for {stack_name}",
            stack_name=stack_name,
        )

    def _manage_sccs(
        self, cmd: CommandExecutor, context: ExecutionContext,
        plan_config: dict, namespace: str
    ):
        """Add anyuid/privileged SCCs when ``runAsUser: 0`` is configured."""
        # Check if runAsUser: 0 is set in any of the deployment configs
        needs_elevated = False

        # Check standalone config
        standalone_cfg = plan_config.get("standalone", {})
        if standalone_cfg.get("securityContext", {}).get("runAsUser") == 0:
            needs_elevated = True

        # Check modelservice decode/prefill configs
        for role in ["decode", "prefill"]:
            role_cfg = plan_config.get(f"vllmModelservice{role.capitalize()}", {})
            if role_cfg.get("securityContext", {}).get("runAsUser") == 0:
                needs_elevated = True

        # Check common vllm config
        vllm_common = plan_config.get("vllmCommon", {})
        if vllm_common.get("securityContext", {}).get("runAsUser") == 0:
            needs_elevated = True

        if not needs_elevated:
            context.logger.log_info(
                "ℹ️  No runAsUser:0 detected — skipping SCC assignment"
            )
            return

        sa_name = (
            plan_config.get("serviceAccount", {}).get("name", "default")
        )
        for scc in ["anyuid", "privileged"]:
            cmd.oc(
                "adm", "policy", "add-scc-to-user",
                scc, "-z", sa_name, "-n", namespace,
            )

    def _collect_logs(
        self, cmd: CommandExecutor, context: ExecutionContext,
        namespace: str
    ):
        """Collect decode and prefill pod logs after deployment."""
        logs_dir = context.setup_logs_dir()
        for role in ["decode", "prefill"]:
            result = cmd.kubectl(
                "get", "pods",
                "-l", f"llm-d.ai/role={role}",
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

    def _install_wva(  # pylint: disable=too-many-arguments
        self,
        cmd: CommandExecutor,
        context: ExecutionContext,
        plan_config: dict,
        errors: list,
    ):
        """Install Workload Variant Autoscaler components."""
        namespace = context.namespace or "default"

        # Install WVA via helm
        wva_chart = plan_config.get("helmRepositories", {}).get(
            "wva", {}
        )
        chart_url = wva_chart.get("url", "")
        chart_version = plan_config.get("chartVersions", {}).get(
            "wva", ""
        )

        if chart_url and chart_version:
            result = cmd.helm(
                "upgrade", "--install", "wva",
                f"{chart_url}/workload-variant-autoscaler",
                "--version", chart_version,
                "--namespace", namespace,
                "--create-namespace",
                "--wait",
            )
            if not result.success:
                errors.append(
                    f"Failed to install WVA: {result.stderr}"
                )

    def _propagate_standup_parameters(
        self, cmd: CommandExecutor, context: ExecutionContext,
        plan_config: dict
    ):
        """Save standup parameters as a ConfigMap for runtime consumption."""
        harness_ns = context.harness_namespace or context.namespace or "default"
        cm_name = "llm-d-benchmark-standup-parameters"

        params = {
            "namespace": context.namespace or "",
            "harness_namespace": harness_ns,
            "deploy_methods": ",".join(context.deployed_methods),
            "platform_type": context.platform_type,
            "cluster_name": context.cluster_name or "",
        }

        if plan_config:
            model_config = plan_config.get("model", {})
            params["model_name"] = model_config.get("name", "")
            params["model_short_name"] = model_config.get("shortName", "")
            vllm_config = plan_config.get("vllmCommon", {})
            params["inference_port"] = str(
                vllm_config.get("inferencePort", 8000)
            )
            params["release"] = plan_config.get("release", "llmdbench")

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
