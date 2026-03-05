"""
Step 07: Deploy Setup (Helm Repositories and Gateway Infrastructure)

Sets up Helm repositories and deploys the gateway infrastructure
needed for modelservice deployments.

This step:
1. Prepares a helm working directory with the correct file layout that
   helmfile expects (infra.yaml, ms-values.yaml, gaie-values.yaml alongside
   the helmfile YAML).
2. Applies the gateway-provider helmfile (Istio base + istiod, or kgateway).
3. Applies the main helmfile infra chart only (--selector name=infra-*).

The modelservice and gaie releases are deployed in later steps (08, 09).

Per-stack step: runs once per rendered stack directory.
"""

import shutil
from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class DeploySetupStep(Step):
    """Set up Helm repositories and deploy gateway infrastructure."""

    # Map from rendered template prefix to the filename helmfile expects.
    # Helmfile references these by relative path in its values: sections.
    _VALUES_FILE_MAP = {
        "11_infra": "infra.yaml",
        "12_gaie-values": "gaie-values.yaml",
        "13_ms-values": "ms-values.yaml",
    }

    def __init__(self):
        super().__init__(
            number=7,
            name="deploy_setup",
            description="Set up Helm repos and gateway infrastructure",
            phase=Phase.STANDUP,
            per_stack=True,
        )

    def should_skip(self, context: ExecutionContext) -> bool:
        # Only needed for modelservice deployments
        return "modelservice" not in context.deployed_methods

    def execute(
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

        # Load stack config for release name
        plan_config = self._load_stack_config(stack_path)
        release = plan_config.get("release", "llmdbench")
        namespace = context.namespace or "default"

        # Prepare helm working directory with properly named value files
        helm_dir = self._prepare_helm_dir(context, stack_path, errors)

        # Apply the gateway provider helmfile (Istio/kgateway).
        # Do NOT pass --namespace here: the releases define their own
        # namespaces (e.g. istio-system) and a mismatched --namespace
        # confuses helmfile's dependency resolution for the needs: field.
        gw_helmfile = self._find_yaml(stack_path, "09_helmfile-gateway-provider")
        if gw_helmfile:
            result = cmd.helmfile(
                "apply", "-f", str(gw_helmfile),
                "--skip-diff-on-install", "--skip-schema-validation",
            )
            if not result.success:
                errors.append(
                    f"Failed to apply gateway helmfile: {result.stderr}"
                )

        # Apply the main helmfile (infra chart only).
        # The helmfile is copied to the helm working dir so that relative
        # value file references (infra.yaml, etc.) resolve correctly.
        main_helmfile = self._find_yaml(stack_path, "10_helmfile-main")
        if main_helmfile and helm_dir:
            # Copy helmfile into the helm working dir
            helmfile_work = helm_dir / "helmfile.yaml"
            shutil.copy2(main_helmfile, helmfile_work)

            # For non-admin users, patch the helmfile to disable namespace creation
            if context.non_admin:
                self._patch_helmfile_for_non_admin(helmfile_work)

            result = cmd.helmfile(
                "--namespace", namespace,
                "--selector", f"name=infra-{release}",
                "apply", "-f", str(helmfile_work),
                "--skip-diff-on-install", "--skip-schema-validation",
            )
            if not result.success:
                errors.append(
                    f"Failed to apply infra helmfile: {result.stderr}"
                )

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Deploy setup had errors",
                errors=errors,
                stack_name=stack_path.name,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=(
                "Helm repos and gateway infrastructure deployed "
                f"for {stack_path.name}"
            ),
            stack_name=stack_path.name,
        )

    def _prepare_helm_dir(
        self, context: ExecutionContext, stack_path: Path, errors: list
    ) -> Path | None:
        """Prepare a helm working directory with value files named as helmfile expects."""
        try:
            helm_dir = context.setup_helm_dir() / stack_path.name
            helm_dir.mkdir(parents=True, exist_ok=True)

            for prefix, target_name in self._VALUES_FILE_MAP.items():
                source = self._find_yaml(stack_path, prefix)
                if source:
                    shutil.copy2(source, helm_dir / target_name)

            return helm_dir
        except OSError as exc:
            errors.append(f"Failed to prepare helm directory: {exc}")
            return None

    def _patch_helmfile_for_non_admin(self, helmfile_path: Path):
        """Prepend ``helmDefaults: createNamespace: false`` for non-admin users."""
        try:
            content = helmfile_path.read_text(encoding="utf-8")
            # Check if helmDefaults already exists
            if "helmDefaults:" not in content:
                patched = (
                    "helmDefaults:\n"
                    "  createNamespace: false\n"
                    "---\n"
                    + content
                )
                helmfile_path.write_text(patched, encoding="utf-8")
        except OSError:
            pass
