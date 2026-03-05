"""
llmdbenchmark.executor.context

Defines the ExecutionContext dataclass that carries state through all
execution phases (standup, run, teardown).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llmdbenchmark.executor.step import Phase


@dataclass
class ExecutionContext:  # pylint: disable=too-many-instance-attributes
    """
    Shared execution context passed through all steps and phases.

    Populated incrementally:
    - Core paths and flags are set at initialization.
    - Kubernetes connection info is resolved at step 00.
    - Namespace info is populated during standup steps 02-05.
    - Deployed endpoints are populated during standup steps 06/09/10.
    - Experiment treatments are populated during the run phase.
    """

    # Core paths
    plan_dir: Path
    workspace: Path
    base_dir: Path | None = None  # project root (for templates, scenarios, etc.)
    rendered_stacks: list[Path] = field(default_factory=list)

    # Execution flags
    dry_run: bool = False
    verbose: bool = False
    non_admin: bool = False
    current_phase: Phase = Phase.STANDUP
    analyze_locally: bool = False  # skip gate for conda (step 01)
    deep_clean: bool = False       # teardown: wipe all resources in namespaces
    release: str = "llmdbench"     # Helm release name prefix

    # Kubernetes connection info (resolved at runtime by step 00)
    cluster_url: str | None = None
    cluster_token: str | None = None
    kubeconfig: str | None = None

    # Platform detection flags (set by step 00)
    is_openshift: bool = False
    is_kind: bool = False
    is_minikube: bool = False
    # Resolved cluster metadata
    cluster_name: str | None = None        # hostname from API server URL
    cluster_server: str | None = None      # full API server URL
    context_name: str | None = None        # kube context name
    username: str | None = None            # current user for labeling

    # Namespace info (populated during standup)
    namespace: str | None = None
    harness_namespace: str | None = None
    wva_namespace: str | None = None

    # Deployed state (populated during standup, consumed by run)
    deployed_endpoints: dict[str, str] = field(default_factory=dict)
    deployed_methods: list[str] = field(default_factory=list)

    # Node resource discovery (populated during step 03)
    accelerator_resource: str | None = None  # e.g. "nvidia.com/gpu"
    network_resource: str | None = None      # e.g. "rdma/rdma_shared_device_a"

    # Experiment state (populated during run)
    experiment_treatments: list[dict] | None = None
    results_dir: Path | None = None

    # Logger (passed from CLI, shared with CommandExecutor)
    logger: Any = field(default=None, repr=False)

    # Command paths (auto-detected)
    kubectl_cmd: str = "kubectl"
    helm_cmd: str = "helm"
    helmfile_cmd: str = "helmfile"
    python_cmd: str = "python3"

    @property
    def platform_type(self) -> str:
        """Return a human-readable platform label based on detection flags."""
        if self.is_openshift:
            return "OpenShift"
        if self.is_kind:
            return "Kind"
        if self.is_minikube:
            return "Minikube"
        return "Kubernetes"

    def setup_commands_dir(self) -> Path:
        """Return the path for storing command logs, creating it if needed."""
        commands_dir = self.workspace / "setup" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)
        return commands_dir

    def setup_yamls_dir(self) -> Path:
        """Return the path for storing generated YAMLs, creating it if needed."""
        yamls_dir = self.workspace / "setup" / "yamls"
        yamls_dir.mkdir(parents=True, exist_ok=True)
        return yamls_dir

    def setup_logs_dir(self) -> Path:
        """Return the path for storing step logs, creating it if needed."""
        logs_dir = self.workspace / "setup" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir

    def setup_helm_dir(self) -> Path:
        """Return the path for storing helm configs, creating it if needed."""
        helm_dir = self.workspace / "setup" / "helm"
        helm_dir.mkdir(parents=True, exist_ok=True)
        return helm_dir

    def environment_dir(self) -> Path:
        """Return the path for storing environment snapshots, creating it if needed."""
        env_dir = self.workspace / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)
        return env_dir

    def preprocess_dir(self) -> Path | None:
        """Return the path to the preprocess scripts directory.

        Looks for ``llmdbenchmark/standup/preprocess/`` relative to the
        package installation (resolved via ``__file__``).  Falls back to
        ``base_dir/standup/preprocess`` if ``base_dir`` is set.
        """
        # Primary: resolve relative to the installed package
        pkg_dir = Path(__file__).resolve().parent.parent  # llmdbenchmark/
        d = pkg_dir / "standup" / "preprocess"
        if d.is_dir():
            return d
        # Fallback: relative to base_dir (editable installs, dev setups)
        if self.base_dir:
            d = self.base_dir / "llmdbenchmark" / "standup" / "preprocess"
            if d.is_dir():
                return d
        return None
