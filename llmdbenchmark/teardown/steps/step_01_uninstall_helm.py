"""
Teardown Step 01: Uninstall Helm Releases

Finds and uninstalls Helm releases associated with the llm-d deployment.
Searches both the model namespace and harness namespace for releases
matching the configured release name or model labels.

Also removes:
- OpenShift routes for inference gateways
- Download model jobs

Global step: runs once across both namespaces.
"""

from pathlib import Path

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class UninstallHelmStep(Step):
    """Uninstall Helm releases and associated routes."""

    def __init__(self):
        super().__init__(
            number=1,
            name="uninstall_helm",
            description="Uninstall Helm releases in target namespaces",
            phase=Phase.TEARDOWN,
            per_stack=False,
        )

    def should_skip(self, context: ExecutionContext) -> bool:
        return "modelservice" not in context.deployed_methods

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

        release = context.release
        namespaces = self._target_namespaces(context)

        for ns in namespaces:
            self._uninstall_releases(cmd, context, ns, release, errors)
            self._delete_openshift_routes(cmd, context, ns, release)
            self._delete_download_job(cmd, context, ns)

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Helm uninstall had errors",
                errors=errors,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message="Helm releases uninstalled",
        )

    def _target_namespaces(self, context: ExecutionContext) -> list[str]:
        """Return the deduplicated list of namespaces to clean."""
        ns = context.namespace or "default"
        harness_ns = context.harness_namespace or ns
        seen = []
        for n in [ns, harness_ns]:
            if n not in seen:
                seen.append(n)
        return seen

    def _uninstall_releases(
        self, cmd: CommandExecutor, context: ExecutionContext,
        namespace: str, release: str, errors: list
    ):
        """Find and uninstall Helm releases matching the release name."""
        result = cmd.helm(
            "list", "--namespace", namespace, "--no-headers",
        )
        if not result.success:
            return

        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            release_name = parts[0]
            # Match releases belonging to this deployment
            if release in release_name:
                context.logger.log_info(
                    f"Uninstalling Helm release \"{release_name}\" "
                    f"from {namespace}"
                )
                uninstall = cmd.helm(
                    "uninstall", release_name, "--namespace", namespace,
                )
                if not uninstall.success:
                    errors.append(
                        f"Failed to uninstall {release_name}: "
                        f"{uninstall.stderr}"
                    )

    def _delete_openshift_routes(
        self, cmd: CommandExecutor, context: ExecutionContext,
        namespace: str, release: str
    ):
        """Delete OpenShift routes for the inference gateway."""
        if not context.is_openshift:
            return

        for route_name in [
            f"infra-{release}-inference-gateway",
            f"{release}-inference-gateway",
        ]:
            cmd.kubectl(
                "delete", "--namespace", namespace,
                "--ignore-not-found=true",
                "route", route_name,
            )

    def _delete_download_job(
        self, cmd: CommandExecutor, context: ExecutionContext,
        namespace: str
    ):
        """Delete the model download job."""
        cmd.kubectl(
            "delete", "--namespace", namespace,
            "--ignore-not-found=true",
            "job", "download-model",
        )
