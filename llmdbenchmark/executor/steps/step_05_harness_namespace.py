"""
Step 05: Harness Namespace Preparation

Prepares the benchmark harness namespace with:
- Harness namespace creation
- HuggingFace token secret in harness namespace
- Workload PVC(s) for storing benchmark data and results
- Data access pod (rsync daemon) for transferring data
- Service exposing the data access pod
- Preprocesses ConfigMap for vLLM pod scripts
"""

import base64
from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class HarnessNamespaceStep(Step):
    """Prepare the harness namespace with PVC and data access pod."""

    def __init__(self):
        super().__init__(
            number=5,
            name="harness_namespace",
            description="Prepare harness namespace (PVC, data access pod)",
            phase=Phase.STANDUP,
            per_stack=False,
        )

    def execute(
        self, context: ExecutionContext, stack_path: Path | None = None
    ) -> StepResult:
        errors = []
        cmd = CommandExecutor(
            work_dir=context.workspace,
            dry_run=context.dry_run,
            verbose=context.verbose,
            logger=context.logger,
            kubeconfig=context.kubeconfig,
            openshift=context.is_openshift,
        )

        # Load plan config for harness settings
        plan_config = self._load_plan_config(context)

        # 0. Extract harness namespace from config
        harness_ns = None
        if plan_config:
            harness_ns = plan_config.get("harness", {}).get(
                "namespace",
                plan_config.get("namespace", {}).get("name", "")
            )
        if not harness_ns:
            harness_ns = context.namespace or "default"
        context.harness_namespace = harness_ns

        # 1. Create harness namespace
        self._create_harness_namespace(cmd, context, harness_ns, errors)

        # 2. Create HF token secret in harness namespace
        self._create_hf_token_secret(cmd, context, plan_config, harness_ns, errors)

        # 3. Create workload PVC(s)
        pvc_yaml = self._find_rendered_yaml(context, "01_pvc_workload-pvc")
        if pvc_yaml:
            result = cmd.kubectl("apply", "-f", str(pvc_yaml))
            if not result.success and "AlreadyExists" not in result.stderr:
                errors.append(
                    f"Failed to create workload PVC: {result.stderr}"
                )

        # 4. Create data access pod (rsync daemon)
        pod_yaml = self._find_rendered_yaml(
            context, "06_pod_access_to_harness_data"
        )
        if pod_yaml:
            result = cmd.kubectl("apply", "-f", str(pod_yaml))
            if not result.success:
                errors.append(
                    f"Failed to create data access pod: {result.stderr}"
                )

        # 5. Create service for data access
        svc_yaml = self._find_rendered_yaml(
            context, "07_service_access_to_harness_data"
        )
        if svc_yaml:
            result = cmd.kubectl("apply", "-f", str(svc_yaml))
            if not result.success:
                errors.append(
                    f"Failed to create data access service: {result.stderr}"
                )

        # 6. Create preprocesses ConfigMap
        self._create_preprocesses_configmap(cmd, context, harness_ns, errors)

        # Wait for the data access pod to be ready
        wait_result = cmd.kubectl_wait_for_pods(
            label="role=llm-d-benchmark-data-access",
            namespace=harness_ns,
            timeout=120,
            poll_interval=5,
            description="harness data-access pod",
        )
        if not wait_result.success:
            errors.append(
                f"Data access pod not ready: {wait_result.stderr}"
            )

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Harness namespace preparation had errors",
                errors=errors,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=f"Harness namespace prepared (ns={harness_ns})",
        )

    def _create_harness_namespace(
        self, cmd: CommandExecutor, context: ExecutionContext,
        harness_ns: str, errors: list
    ):
        """Create the harness namespace if it doesn't exist."""
        ns_yaml = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {harness_ns}
"""
        yaml_path = context.setup_yamls_dir() / "harness-namespace.yaml"
        yaml_path.write_text(ns_yaml, encoding="utf-8")
        result = cmd.kubectl("apply", "-f", str(yaml_path))
        if not result.success and "AlreadyExists" not in result.stderr:
            errors.append(
                f"Failed to create harness namespace: {result.stderr}"
            )

    def _create_hf_token_secret(
        self, cmd: CommandExecutor, context: ExecutionContext,
        plan_config: dict | None, harness_ns: str, errors: list
    ):
        """Create the HuggingFace token secret in the harness namespace.

        The model namespace already has this secret (from step 04); this
        copies it into the harness namespace so benchmark pods can pull
        gated models too.
        """
        if not plan_config:
            return

        hf_token_name = (
            plan_config.get("vllmCommon", {})
            .get("hfTokenName", "hf-token")
        )
        hf_token_key = (
            plan_config.get("vllmCommon", {})
            .get("hfTokenKey", "HF_TOKEN")
        )

        # Check if the secret already exists in the harness namespace
        check = cmd.kubectl(
            "get", "secret", hf_token_name,
            "--namespace", harness_ns,
            "--ignore-not-found",
        )
        if check.success and check.stdout.strip():
            context.logger.log_info(
                f"✅ HF token secret already exists in {harness_ns}"
            )
            return

        # Try to copy from model namespace
        model_ns = context.namespace or "default"
        get_result = cmd.kubectl(
            "get", "secret", hf_token_name,
            "--namespace", model_ns,
            "-o", "yaml",
        )
        if get_result.success and get_result.stdout.strip():
            # Modify namespace and apply
            try:
                secret_doc = yaml.safe_load(get_result.stdout)
                if secret_doc:
                    secret_doc["metadata"]["namespace"] = harness_ns
                    # Remove resource version for clean apply
                    secret_doc["metadata"].pop("resourceVersion", None)
                    secret_doc["metadata"].pop("uid", None)
                    secret_doc["metadata"].pop("creationTimestamp", None)
                    managed = secret_doc["metadata"].pop("managedFields", None)

                    yaml_path = (
                        context.setup_yamls_dir() / "harness-hf-secret.yaml"
                    )
                    with open(yaml_path, "w", encoding="utf-8") as f:
                        yaml.dump(secret_doc, f, default_flow_style=False)

                    apply_result = cmd.kubectl(
                        "apply", "-f", str(yaml_path)
                    )
                    if apply_result.success:
                        context.logger.log_info(
                            f"✅ HF token secret created in {harness_ns}"
                        )
                    else:
                        context.logger.log_warning(
                            f"Could not create HF secret in harness ns: "
                            f"{apply_result.stderr}"
                        )
            except (yaml.YAMLError, KeyError) as exc:
                context.logger.log_warning(
                    f"Could not copy HF secret to harness ns: {exc}"
                )

    def _create_preprocesses_configmap(
        self, cmd: CommandExecutor, context: ExecutionContext,
        harness_ns: str, errors: list
    ):
        """Create a ConfigMap from ``setup/preprocess/*`` scripts in the harness namespace."""
        preprocess_dir = context.preprocess_dir()
        config_map_name = "llm-d-benchmark-preprocesses"

        context.logger.log_info(
            "🚚 Creating configmap with preprocess scripts..."
        )

        if not preprocess_dir or not preprocess_dir.is_dir():
            context.logger.log_warning(
                "Preprocess directory not found — creating empty ConfigMap"
            )
            result = cmd.kubectl(
                "create", "configmap", config_map_name,
                "--namespace", harness_ns,
                "--dry-run=client", "-o", "yaml",
            )
            if result.success:
                yaml_path = (
                    context.setup_yamls_dir()
                    / "preprocesses-configmap-empty.yaml"
                )
                yaml_path.write_text(result.stdout, encoding="utf-8")
                cmd.kubectl("apply", "-f", str(yaml_path))
            return

        # Build --from-file args for each file in the preprocess directory
        from_file_args = []
        try:
            file_paths = sorted(
                p for p in preprocess_dir.rglob("*") if p.is_file()
            )
            for path in file_paths:
                from_file_args.extend([
                    f"--from-file={path.name}={path}",
                ])
        except OSError as exc:
            context.logger.log_warning(
                f"Error reading preprocess directory: {exc}"
            )

        if not from_file_args:
            context.logger.log_info(
                "No preprocess files found — creating empty ConfigMap"
            )
            return

        # Generate the configmap YAML via dry-run, then apply
        create_args = [
            "create", "configmap", config_map_name,
            "--namespace", harness_ns,
        ] + from_file_args + ["--dry-run=client", "-o", "yaml"]

        result = cmd.kubectl(*create_args)
        if result.success:
            yaml_path = (
                context.setup_yamls_dir() / "preprocesses-configmap.yaml"
            )
            yaml_path.write_text(result.stdout, encoding="utf-8")
            apply_result = cmd.kubectl("apply", "-f", str(yaml_path))
            if not apply_result.success:
                context.logger.log_warning(
                    f"Failed to apply preprocesses configmap: "
                    f"{apply_result.stderr}"
                )
        else:
            context.logger.log_warning(
                f"Failed to generate preprocesses configmap: {result.stderr}"
            )
