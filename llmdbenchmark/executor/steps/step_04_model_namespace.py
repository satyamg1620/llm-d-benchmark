"""
Step 04: Model Namespace Preparation

Prepares the model (vLLM common) namespace with:
- Namespace, ServiceAccount, RBAC, and HuggingFace secret
- PVC for model artifact caching
- Extra PVC (optional, e.g. for Spyre precompiled models)
- Context secret for downstream cluster access
- Preprocesses ConfigMap (scripts mounted into vLLM pods)
- Model download job to pull model weights from HuggingFace
"""

import json
from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class ModelNamespaceStep(Step):
    """Prepare the model namespace with PVC, secrets, and download job."""

    def __init__(self):
        super().__init__(
            number=4,
            name="model_namespace",
            description="Prepare model namespace (PVC, secrets, download job)",
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

        self._apply_namespace_resources(cmd, context, errors)
        self._create_model_pvc(cmd, context, errors)
        self._create_extra_pvc(cmd, context, errors)
        self._add_context_secret(cmd, context, errors)
        self._create_preprocesses_configmap(cmd, context, errors)
        self._launch_download_job(cmd, context, errors)

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Model namespace preparation had errors",
                errors=errors,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=f"Model namespace prepared (ns={context.namespace})",
        )

    def _apply_namespace_resources(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Apply namespace, ServiceAccount, RBAC, and secret resources."""
        ns_yaml = self._find_rendered_yaml(
            context, "05_namespace_sa_rbac_secret"
        )
        if not ns_yaml:
            return

        result = cmd.kubectl("apply", "-f", str(ns_yaml))
        if not result.success:
            errors.append(
                f"Failed to apply namespace resources: {result.stderr}"
            )

        # Extract namespace name from the YAML for context
        try:
            with open(ns_yaml, encoding="utf-8") as f:
                docs = list(yaml.safe_load_all(f))
            for doc in docs:
                if doc and doc.get("kind") == "Namespace":
                    context.namespace = doc["metadata"]["name"]
                    break
        except (yaml.YAMLError, OSError):
            pass

    def _create_model_pvc(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Create the model PVC from rendered YAML."""
        pvc_yaml = self._find_rendered_yaml(context, "02_pvc_model-pvc")
        if not pvc_yaml:
            return

        result = cmd.kubectl("apply", "-f", str(pvc_yaml))
        if not result.success and "AlreadyExists" not in result.stderr:
            errors.append(
                f"Failed to create model PVC: {result.stderr}"
            )

    def _create_extra_pvc(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Create the extra PVC from rendered YAML, if present.

        The rendered template ``16_pvc_extra-pvc.yaml`` will be empty when
        ``storage.extraPvc.name`` is not set, so we skip in that case.
        """
        extra_pvc_yaml = self._find_rendered_yaml(context, "16_pvc_extra-pvc")
        if not extra_pvc_yaml:
            return

        # The template renders to empty/whitespace when extraPvc is disabled
        try:
            content = extra_pvc_yaml.read_text(encoding="utf-8").strip()
            if not content:
                return
        except OSError:
            return

        result = cmd.kubectl("apply", "-f", str(extra_pvc_yaml))
        if not result.success and "AlreadyExists" not in result.stderr:
            errors.append(
                f"Failed to create extra PVC: {result.stderr}"
            )

    def _add_context_secret(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Save the kubeconfig as a Secret (``llm-d-benchmark-context``)
        so downstream pods can access the cluster."""
        namespace = context.namespace or "default"

        # Build the kubeconfig content — use the existing kubeconfig file
        kubeconfig_path = context.kubeconfig
        if not kubeconfig_path:
            # Try the environment context file
            ctx_file = context.environment_dir() / "context.ctx"
            if ctx_file.exists():
                kubeconfig_path = str(ctx_file)

        if not kubeconfig_path:
            context.logger.log_info(
                "ℹ️  No kubeconfig available — skipping context secret"
            )
            return

        # Create the secret from file
        result = cmd.kubectl(
            "create", "secret", "generic",
            "llm-d-benchmark-context",
            f"--from-file=context.ctx={kubeconfig_path}",
            "--namespace", namespace,
            "--dry-run=client", "-o", "yaml",
        )
        if result.success:
            # Pipe through kubectl apply
            apply_result = cmd.execute(
                f"echo '{result.stdout}' | kubectl apply -f -"
            )
            if not apply_result.success:
                context.logger.log_warning(
                    f"Could not create context secret: {apply_result.stderr}"
                )
        else:
            context.logger.log_warning(
                f"Could not generate context secret: {result.stderr}"
            )

    def _create_preprocesses_configmap(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Create a ConfigMap from ``setup/preprocess/*`` scripts.

        The resulting ``llm-d-benchmark-preprocesses`` ConfigMap is mounted
        at ``/setup/preprocess`` in vLLM pods via ``vllmCommon.volumes``.
        """
        preprocess_dir = context.preprocess_dir()
        config_map_name = "llm-d-benchmark-preprocesses"
        namespace = context.harness_namespace or context.namespace or "default"

        context.logger.log_info(
            "🚚 Creating configmap with preprocess scripts..."
        )

        if not preprocess_dir or not preprocess_dir.is_dir():
            context.logger.log_warning(
                f"Preprocess directory not found — creating empty ConfigMap"
            )
            # Create empty configmap
            result = cmd.kubectl(
                "create", "configmap", config_map_name,
                "--namespace", namespace,
                "--dry-run=client", "-o", "yaml",
            )
            if result.success:
                cmd.execute(
                    f"echo '{result.stdout}' | kubectl apply -f -"
                )
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
            "--namespace", namespace,
        ] + from_file_args + ["--dry-run=client", "-o", "yaml"]

        result = cmd.kubectl(*create_args)
        if result.success:
            # Write to temp file and apply (avoids shell quoting issues)
            yaml_path = context.setup_yamls_dir() / "preprocesses-configmap.yaml"
            yaml_path.write_text(result.stdout, encoding="utf-8")
            apply_result = cmd.kubectl("apply", "-f", str(yaml_path))
            if not apply_result.success:
                context.logger.log_warning(
                    f"Failed to apply preprocesses configmap: {apply_result.stderr}"
                )
        else:
            context.logger.log_warning(
                f"Failed to generate preprocesses configmap: {result.stderr}"
            )

    def _launch_download_job(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Launch the model download job and wait for completion."""
        download_yaml = self._find_rendered_yaml(context, "04_download_job")
        if not download_yaml:
            return

        # Clean up any existing download job pods first
        self._cleanup_download_pods(cmd, context)

        self._delete_existing_job(cmd, context, download_yaml)

        result = cmd.kubectl("apply", "-f", str(download_yaml))
        if not result.success:
            errors.append(
                f"Failed to launch model download job: {result.stderr}"
            )
            return

        # Configurable timeout (default 3600s = 1 hour)
        timeout = 3600
        plan_config = self._load_plan_config(context)
        if plan_config:
            timeout = int(
                plan_config.get("storage", {})
                .get("model", {})
                .get("downloadTimeout", timeout)
            )

        max_retries = 2
        for attempt in range(1, max_retries + 1):
            wait_result = cmd.kubectl_wait_for_job(
                job_name="download-model",
                namespace=context.namespace or "default",
                timeout=timeout,
                poll_interval=15,
                description=f"model download (attempt {attempt}/{max_retries})",
            )
            if wait_result.success:
                context.logger.log_info(
                    f"✅ Model download completed (attempt {attempt})"
                )
                return

            if attempt < max_retries:
                context.logger.log_warning(
                    f"⚠️  Model download failed (attempt {attempt}), retrying..."
                )
                # Clean up and re-launch
                self._cleanup_download_pods(cmd, context)
                self._delete_existing_job(cmd, context, download_yaml)
                re_result = cmd.kubectl("apply", "-f", str(download_yaml))
                if not re_result.success:
                    errors.append(
                        f"Failed to re-launch download job: {re_result.stderr}"
                    )
                    return
            else:
                errors.append(
                    f"Model download job did not complete after "
                    f"{max_retries} attempts: {wait_result.stderr}"
                )

    def _cleanup_download_pods(
        self, cmd: CommandExecutor, context: ExecutionContext
    ):
        """Delete completed/failed download job pods before re-launching."""
        namespace = context.namespace or "default"
        cmd.kubectl(
            "delete", "pods",
            "--selector=job-name=download-model",
            "--namespace", namespace,
            "--field-selector=status.phase!=Running",
            "--ignore-not-found",
        )

    def _delete_existing_job(
        self, cmd: CommandExecutor, context: ExecutionContext,
        download_yaml: Path
    ):
        """Delete any existing download job before re-creating it."""
        try:
            with open(download_yaml, encoding="utf-8") as f:
                job_config = yaml.safe_load(f)

            if job_config:
                job_name = job_config.get(
                    "metadata", {}
                ).get("name", "download-model")
                job_ns = job_config.get(
                    "metadata", {}
                ).get("namespace", context.namespace or "default")

                cmd.kubectl(
                    "delete", "job", job_name,
                    "--namespace", job_ns,
                    "--ignore-not-found",
                )
        except (yaml.YAMLError, OSError):
            pass
