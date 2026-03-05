"""
Teardown Step 03: Delete Namespaced Resources

Removes Kubernetes resources from the model and harness namespaces.

Two modes:
- **Normal mode**: Queries for resources by type, filters by deployment
  method patterns (standalone vs modelservice), and deletes individually.
  Preserves system ConfigMaps and the HuggingFace token secret.
- **Deep mode** (``--deep``): Deletes ALL resources of each kind using
  ``--all``, leaving the namespaces empty.

Global step: runs once across both namespaces.
"""

from pathlib import Path

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


# Resource types to query in normal mode
NORMAL_RESOURCE_LIST = (
    "deployment,httproute,service,gateway,gatewayparameters,"
    "inferencepool,inferencemodel,configmap,ingress,pod,job"
)

# System ConfigMaps and secrets that should not be deleted in normal mode
SYSTEM_EXCLUDES = {
    "kube-root-ca.crt",
    "odh-trusted-ca-bundle",
    "openshift-service-ca.crt",
}

# Patterns matching standalone-specific resources
STANDALONE_PATTERNS = [
    "standalone", "download-model", "testinference", "lmbenchmark",
]

# Patterns matching modelservice-specific resources
MODELSERVICE_PATTERNS = [
    "llm-d-benchmark-preprocesses", "p2p", "inference-gateway",
    "inferencepool", "httproute", "inferencepools.inference.networking.k8s.io",
    "llm-route", "base-model", "endpoint-picker", "inference-route",
    "inference-gateway-secret", "inference-gateway-params",
    "lmbenchmark",
]

# Resource kinds deleted exhaustively in deep mode
DEEP_RESOURCE_KINDS = [
    "deployment", "service", "secret", "gateway", "inferencemodel",
    "inferencepool", "httproute", "configmap", "job", "role",
    "rolebinding", "serviceaccount", "hpa", "va", "servicemonitor",
    "podmonitor", "pod", "pvc",
]

# Additional resource kinds on OpenShift
OPENSHIFT_RESOURCE_KINDS = ["route"]


class DeleteResourcesStep(Step):
    """Delete namespaced resources (normal or deep mode)."""

    def __init__(self):
        super().__init__(
            number=3,
            name="delete_resources",
            description="Delete namespaced resources",
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

        namespaces = self._target_namespaces(context)

        if context.deep_clean:
            self._deep_clean(cmd, context, namespaces, errors)
        else:
            self._normal_clean(cmd, context, namespaces, errors)

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Resource deletion had errors",
                errors=errors,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=(
                f"Namespaced resources deleted "
                f"({'deep' if context.deep_clean else 'normal'} mode)"
            ),
        )

    def _target_namespaces(self, context: ExecutionContext) -> list[str]:
        """Return deduplicated list of namespaces to clean."""
        ns = context.namespace or "default"
        harness_ns = context.harness_namespace or ns
        seen = []
        for n in [ns, harness_ns]:
            if n not in seen:
                seen.append(n)
        return seen

    def _deep_clean(
        self, cmd: CommandExecutor, context: ExecutionContext,
        namespaces: list[str], errors: list
    ):
        """Delete all resources of each kind in both namespaces."""
        kinds = list(DEEP_RESOURCE_KINDS)
        if context.is_openshift:
            kinds.extend(OPENSHIFT_RESOURCE_KINDS)

        for ns in namespaces:
            context.logger.log_info(
                f"Deep cleaning namespace \"{ns}\" "
                f"({len(kinds)} resource kinds)..."
            )
            for kind in kinds:
                result = cmd.kubectl(
                    "delete", kind, "--all",
                    "--namespace", ns,
                    "--ignore-not-found=true",
                )
                # Log but don't treat CRD-missing errors as failures
                if not result.success:
                    stderr_lower = result.stderr.lower()
                    if (
                        "the server doesn't have a resource type"
                        not in stderr_lower
                        and "not found" not in stderr_lower
                        and "no matches for kind" not in stderr_lower
                    ):
                        errors.append(
                            f"Failed to delete {kind} in {ns}: "
                            f"{result.stderr}"
                        )

    def _normal_clean(
        self, cmd: CommandExecutor, context: ExecutionContext,
        namespaces: list[str], errors: list
    ):
        """Query for resources, filter by deployment method, delete individually."""
        resource_list = NORMAL_RESOURCE_LIST
        if context.is_openshift:
            resource_list += ",route"

        # Prune resource types not supported by this cluster
        resource_list = self._prune_unsupported(cmd, resource_list)

        standalone_active = "standalone" in context.deployed_methods
        modelservice_active = "modelservice" in context.deployed_methods

        # Load plan config to get HF token secret name
        plan_config = self._load_plan_config(context)
        hf_secret = "llm-d-hf-token"
        if plan_config:
            hf_secret = (
                plan_config.get("vllmCommon", {})
                .get("hfTokenName", hf_secret)
            )

        for ns in namespaces:
            context.logger.log_info(
                f"Cleaning namespace \"{ns}\" (normal mode)..."
            )

            # Get all resources
            result = cmd.kubectl(
                "get", resource_list,
                "--namespace", ns,
                "-o", "name",
            )
            if not result.success or not result.stdout.strip():
                continue

            all_resources = result.stdout.strip().splitlines()

            # Filter out system resources and HF token
            filtered = [
                r for r in all_resources
                if not self._is_system_resource(r, hf_secret)
            ]

            # Apply deployment method filter
            if standalone_active and not modelservice_active:
                filtered = [
                    r for r in filtered
                    if self._matches_any(r, STANDALONE_PATTERNS)
                ]
            elif modelservice_active and not standalone_active:
                filtered = [
                    r for r in filtered
                    if self._matches_any(r, MODELSERVICE_PATTERNS)
                ]
            # If both active (or neither specified), delete all non-system resources

            for resource in filtered:
                del_result = cmd.kubectl(
                    "delete", "--namespace", ns,
                    "--ignore-not-found=true",
                    resource,
                )
                if not del_result.success:
                    context.logger.log_warning(
                        f"Could not delete {resource}: {del_result.stderr}"
                    )

    def _prune_unsupported(
        self, cmd: CommandExecutor, resource_list: str
    ) -> str:
        """Remove resource types that the cluster does not support."""
        supported = []
        for resource_type in resource_list.split(","):
            resource_type = resource_type.strip()
            if not resource_type:
                continue
            check = cmd.kubectl(
                "get", resource_type,
                "--no-headers", "-o", "name",
            )
            if check.success or "No resources found" in check.stderr:
                supported.append(resource_type)
        return ",".join(supported)

    @staticmethod
    def _is_system_resource(resource: str, hf_secret: str) -> bool:
        """Return True if this resource should be preserved."""
        resource_lower = resource.lower()
        for exclude in SYSTEM_EXCLUDES:
            if exclude in resource_lower:
                return True
        if f"secret/{hf_secret}" in resource_lower:
            return True
        return False

    @staticmethod
    def _matches_any(resource: str, patterns: list[str]) -> bool:
        """Return True if the resource name matches any of the patterns."""
        resource_lower = resource.lower()
        return any(p.lower() in resource_lower for p in patterns)
