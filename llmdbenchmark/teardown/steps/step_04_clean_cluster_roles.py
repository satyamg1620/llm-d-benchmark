"""
Teardown Step 04: Clean Cluster-Scoped Resources

Removes cluster-scoped resources created by the modelservice deployment:
- ClusterRoleBindings matching the release name
- ClusterRoles matching the release name
- Well-known ClusterRoles created by the modelservice Helm chart

Requires admin privileges. Skipped when ``--non-admin`` is set or when
the deployment method is standalone-only.

Global step: runs once.
"""

from pathlib import Path

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor

# Well-known ClusterRole suffixes created by the modelservice chart
_MODELSERVICE_ROLE_SUFFIXES = [
    "modelservice-endpoint-picker",
    "modelservice-epp-metrics-scrape",
    "modelservice-manager",
    "modelservice-metrics-auth",
    "modelservice-admin",
    "modelservice-editor",
    "modelservice-viewer",
]


class CleanClusterRolesStep(Step):
    """Remove cluster-scoped ClusterRoles and ClusterRoleBindings."""

    def __init__(self):
        super().__init__(
            number=4,
            name="clean_cluster_roles",
            description="Remove cluster-scoped roles and bindings (admin only)",
            phase=Phase.TEARDOWN,
            per_stack=False,
        )

    def should_skip(self, context: ExecutionContext) -> bool:
        if context.non_admin:
            return True
        if "modelservice" not in context.deployed_methods:
            return True
        return False

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

        # 1. Delete ClusterRoleBindings matching the release name
        self._delete_matching(cmd, context, "ClusterRoleBinding", release, errors)

        # 2. Delete ClusterRoles matching the release name
        self._delete_matching(cmd, context, "ClusterRole", release, errors)

        # 3. Delete well-known ClusterRoles by explicit name
        for suffix in _MODELSERVICE_ROLE_SUFFIXES:
            cr_name = f"{release}-{suffix}"
            cmd.kubectl(
                "delete", "--ignore-not-found=true",
                "ClusterRole", cr_name,
            )

        # 4. Delete HTTPRoutes for model labels (OpenShift)
        if context.is_openshift:
            namespaces = self._target_namespaces(context)
            for ns in namespaces:
                # Query httproutes in the namespace and delete ones
                # matching the release
                result = cmd.kubectl(
                    "get", "httproute",
                    "--namespace", ns,
                    "-o", "name",
                    "--ignore-not-found",
                )
                if result.success and result.stdout.strip():
                    for route in result.stdout.strip().splitlines():
                        cmd.kubectl(
                            "delete", "--namespace", ns,
                            "--ignore-not-found=true",
                            route,
                        )

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Cluster role cleanup had errors",
                errors=errors,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message="Cluster-scoped resources cleaned",
        )

    def _target_namespaces(self, context: ExecutionContext) -> list[str]:
        """Return deduplicated list of namespaces."""
        ns = context.namespace or "default"
        harness_ns = context.harness_namespace or ns
        seen = []
        for n in [ns, harness_ns]:
            if n not in seen:
                seen.append(n)
        return seen

    def _delete_matching(
        self, cmd: CommandExecutor, context: ExecutionContext,
        kind: str, release: str, errors: list
    ):
        """Delete cluster-scoped resources of a given kind matching the release name."""
        result = cmd.kubectl(
            "get", kind, "--no-headers", "-o", "name",
        )
        if not result.success or not result.stdout.strip():
            return

        for line in result.stdout.strip().splitlines():
            resource_name = line.strip()
            # Match resources containing the release name
            if release in resource_name:
                context.logger.log_info(
                    f"Deleting {kind}: {resource_name}"
                )
                del_result = cmd.kubectl(
                    "delete", "--ignore-not-found=true", resource_name,
                )
                if not del_result.success:
                    errors.append(
                        f"Failed to delete {resource_name}: "
                        f"{del_result.stderr}"
                    )
