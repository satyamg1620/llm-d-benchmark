"""
Teardown Step 02: Clean Harness Resources

Removes benchmark harness resources from the harness namespace:
- Workload profile ConfigMaps
- Load generator pods
- Preprocesses ConfigMap
- Standup parameters ConfigMap
- Context secret

Global step: runs once targeting the harness namespace.
"""

from pathlib import Path

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class CleanHarnessStep(Step):
    """Remove harness resources (ConfigMaps, pods, secrets)."""

    def __init__(self):
        super().__init__(
            number=2,
            name="clean_harness",
            description="Remove harness resources (ConfigMaps, pods, secrets)",
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

        harness_ns = context.harness_namespace or context.namespace or "default"

        # 1. Delete workload profile ConfigMaps
        self._delete_profile_configmaps(cmd, context, harness_ns)

        # 2. Delete load generator pods
        context.logger.log_info("Deleting load generator pods...")
        cmd.kubectl(
            "delete", "pod",
            "-l", "app=llmdbench-harness-launcher,function=load_generator",
            "--namespace", harness_ns,
            "--ignore-not-found",
        )

        # 3. Delete well-known ConfigMaps
        for cm_name in [
            "llm-d-benchmark-preprocesses",
            "llm-d-benchmark-standup-parameters",
        ]:
            cmd.kubectl(
                "delete", "configmap", cm_name,
                "--namespace", harness_ns,
                "--ignore-not-found",
            )

        # 4. Delete context secret
        cmd.kubectl(
            "delete", "secret", "llm-d-benchmark-context",
            "--namespace", harness_ns,
            "--ignore-not-found",
        )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=f"Harness resources cleaned (ns={harness_ns})",
        )

    def _delete_profile_configmaps(
        self, cmd: CommandExecutor, context: ExecutionContext,
        namespace: str
    ):
        """Delete workload profile ConfigMaps.

        Queries for ConfigMaps matching the ``*-profiles`` pattern.
        """
        result = cmd.kubectl(
            "get", "configmap",
            "--namespace", namespace,
            "-o", "jsonpath={.items[*].metadata.name}",
        )
        if not result.success or not result.stdout.strip():
            return

        for cm_name in result.stdout.strip().split():
            if cm_name.endswith("-profiles"):
                context.logger.log_info(
                    f"Deleting profile ConfigMap \"{cm_name}\""
                )
                cmd.kubectl(
                    "delete", "configmap", cm_name,
                    "--namespace", namespace,
                    "--ignore-not-found",
                )
