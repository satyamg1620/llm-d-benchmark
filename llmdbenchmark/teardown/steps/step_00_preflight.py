"""
Teardown Step 00: Preflight Checks

Validates cluster connectivity and loads the plan config to determine
what namespaces, release names, and deployment methods need to be
torn down. Populates the ExecutionContext with namespace info and
platform detection flags.

Global step: runs once before any teardown operations.
"""

from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class TeardownPreflightStep(Step):
    """Validate cluster access and load teardown configuration."""

    def __init__(self):
        super().__init__(
            number=0,
            name="teardown_preflight",
            description="Validate cluster connectivity and load teardown config",
            phase=Phase.TEARDOWN,
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

        # 1. Verify cluster connectivity
        result = cmd.kubectl("cluster-info")
        if not result.success and not context.dry_run:
            errors.append(f"Cannot connect to cluster: {result.stderr}")
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Cluster connectivity check failed",
                errors=errors,
            )

        # 2. Detect platform (OpenShift vs vanilla K8s)
        if not context.dry_run:
            oc_check = cmd.execute("oc version --client 2>/dev/null")
            if oc_check.success:
                api_check = cmd.execute(
                    "oc api-versions 2>/dev/null | grep openshift"
                )
                if api_check.success and api_check.stdout.strip():
                    context.is_openshift = True
                    context.logger.log_info(
                        "Detected OpenShift cluster"
                    )

        # 3. Load plan config to get namespace and release info
        plan_config = self._load_plan_config(context)
        if plan_config:
            ns_config = plan_config.get("namespace", {})
            context.namespace = (
                context.namespace or ns_config.get("name", "llmdbench")
            )
            context.harness_namespace = (
                context.harness_namespace
                or plan_config.get("harness", {}).get("namespace", "")
                or context.namespace
            )
            context.release = plan_config.get("release", context.release)

            context.logger.log_info(
                f"Target namespaces: {context.namespace}, {context.harness_namespace}"
            )
            context.logger.log_info(
                f"Helm release: {context.release}"
            )
        else:
            # No plan config available — use context defaults
            if not context.namespace:
                context.namespace = "llmdbench"
            if not context.harness_namespace:
                context.harness_namespace = context.namespace
            context.logger.log_warning(
                "No plan config found — using default namespaces"
            )

        context.logger.log_info(
            f"Deploy methods to tear down: {context.deployed_methods}"
        )

        if context.deep_clean:
            context.logger.log_info(
                "Deep cleaning enabled — all resources in target namespaces will be removed"
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=(
                f"Preflight complete "
                f"(ns={context.namespace}, platform={context.platform_type})"
            ),
        )
