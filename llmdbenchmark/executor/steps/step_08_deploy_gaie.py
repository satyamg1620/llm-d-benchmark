"""
Step 08: Deploy GAIE (Gateway API Inference Extension)

Deploys the GAIE components including the inference pool and
endpoint picker (EPP) via the helmfile configuration.

Per-stack step: runs once per rendered stack directory.
"""

import shutil
from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class DeployGaieStep(Step):
    """Deploy the GAIE inference extension components."""

    def __init__(self):
        super().__init__(
            number=8,
            name="deploy_gaie",
            description="Deploy GAIE inference extension",
            phase=Phase.STANDUP,
            per_stack=True,
        )

    def should_skip(self, context: ExecutionContext) -> bool:
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

        # The GAIE values file must exist
        gaie_values = self._find_yaml(stack_path, "12_gaie-values")

        if not gaie_values:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=True,
                message="No GAIE values found, skipping",
                stack_name=stack_path.name,
            )

        # Load stack config for release name
        plan_config = self._load_stack_config(stack_path)
        release = plan_config.get("release", "llmdbench")
        namespace = context.namespace or "default"
        stack_name = stack_path.name

        # Use the helm working directory (prepared by step 07)
        helm_dir = context.setup_helm_dir() / stack_name
        helmfile_work = helm_dir / "helmfile.yaml"

        if helmfile_work.exists():
            # Deploy GAIE via helmfile using selector
            model_short = plan_config.get("model", {}).get("shortName", stack_name)
            result = cmd.helmfile(
                "--namespace", namespace,
                "--selector", f"name={model_short}-gaie",
                "apply", "-f", str(helmfile_work),
                "--skip-diff-on-install", "--skip-schema-validation",
            )
            if not result.success:
                errors.append(f"Failed to deploy GAIE: {result.stderr}")
        else:
            # Fallback: try original helmfile locations
            main_helmfile = self._find_yaml(stack_path, "10_helmfile-main")
            if main_helmfile:
                model_short = plan_config.get("model", {}).get("shortName", stack_name)
                result = cmd.helmfile(
                    "--namespace", namespace,
                    "--selector", f"name={model_short}-gaie",
                    "apply", "-f", str(main_helmfile),
                    "--skip-diff-on-install", "--skip-schema-validation",
                )
                if not result.success:
                    errors.append(f"Failed to deploy GAIE: {result.stderr}")

        # Wait for infrastructure gateway pods first
        if not errors:
            gw_wait = cmd.kubectl_wait_for_pods(
                label="app.kubernetes.io/name=llm-d-infra",
                namespace=namespace,
                timeout=300,
                poll_interval=10,
                description="infrastructure gateway pods",
            )
            if not gw_wait.success:
                # Non-fatal: gateway pods may already be up from step 07
                if "no pods found" not in gw_wait.stderr.lower():
                    context.logger.log_warning(
                        f"Gateway pods not ready: {gw_wait.stderr}"
                    )

        # Wait for GAIE endpoint-picker pods
        if not errors:
            epp_wait = cmd.kubectl_wait_for_pods(
                label="app.kubernetes.io/component=endpoint-picker",
                namespace=namespace,
                timeout=300,
                poll_interval=10,
                description="GAIE endpoint-picker",
            )
            if not epp_wait.success:
                errors.append(f"GAIE pods not ready: {epp_wait.stderr}")

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="GAIE deployment had errors",
                errors=errors,
                stack_name=stack_path.name,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=f"GAIE deployed for {stack_path.name}",
            stack_name=stack_path.name,
        )
