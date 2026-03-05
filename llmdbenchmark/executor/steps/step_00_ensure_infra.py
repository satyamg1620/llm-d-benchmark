"""
Step 00: Ensure LLM-D Infrastructure

Validates system dependencies (kubectl, helm, helmfile, etc.) and establishes
Kubernetes cluster connectivity.  Extracts a self-contained kubeconfig
(``context.ctx``) that is reused by every subsequent step and can later be
stored as a Kubernetes Secret so harness pods also have cluster access.

Kubeconfig resolution priority:
1. If a stale ``context.ctx`` exists (different server), it is removed.
2. A new ``context.ctx`` is created via one of:
   a) Copy from an explicit ``--kubeconfig`` CLI path.
   b) Copy from ``~/.kube/config-{cluster_name}`` (named config convention).
   c) Extract the current context: ``kubectl config view --minify --flatten --raw``.
   d) ``oc login`` with ``--cluster-url`` / ``--cluster-token`` for OpenShift.
3. ``context.kubeconfig`` is set to ``context.ctx`` so all ``CommandExecutor``
   instances automatically pick it up.
"""

import json
import shutil
import subprocess
from pathlib import Path

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.deps import check_system_dependencies, check_python_version

try:
    from llmdbenchmark.utilities.kubernetes import kube_connect, is_openshift
    from kubernetes.client.rest import ApiException
    from kubernetes.config import ConfigException

    _KUBE_AVAILABLE = True
except ImportError:
    _KUBE_AVAILABLE = False


class EnsureInfraStep(Step):
    """Validate system dependencies and Kubernetes cluster connectivity."""

    def __init__(self):
        super().__init__(
            number=0,
            name="ensure_infra",
            description="Validate system dependencies and cluster connectivity",
            phase=Phase.STANDUP,
            per_stack=False,
        )

    def execute(
        self, context: ExecutionContext, stack_path: Path | None = None
    ) -> StepResult:
        errors = []

        # Check Python version
        py_ok, py_version = check_python_version()
        if not py_ok:
            errors.append(f"Python >= 3.11 required, found {py_version}")

        # Check system dependencies
        dep_result = check_system_dependencies()
        if dep_result.has_missing_required:
            errors.append(
                f"Missing required tools: {', '.join(dep_result.missing_required)}"
            )

        if dep_result.missing_optional:
            # Log warnings but don't fail
            if context.logger:
                for tool in dep_result.missing_optional:
                    context.logger.log_warning(
                        f"Optional tool not found: {tool}"
                    )

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Infrastructure checks failed",
                errors=errors,
            )

        # Establish Kubernetes connectivity and store context.ctx
        if not context.dry_run:
            if not _KUBE_AVAILABLE:
                errors.append(
                    "Kubernetes Python client is not installed; "
                    "cannot verify cluster connectivity"
                )
                return StepResult(
                    step_number=self.number,
                    step_name=self.name,
                    success=False,
                    message="Cluster connectivity check failed",
                    errors=errors,
                )

            try:
                api_client = kube_connect(
                    kubeconfig=context.kubeconfig,
                    cluster_url=context.cluster_url,
                    token=context.cluster_token,
                )

                # Detect platform type
                context.is_openshift = is_openshift(api_client)
                self._detect_platform(context)

                # Store kubeconfig as context.ctx
                self._store_kubeconfig(context)

                # Resolve cluster metadata and print banner
                self._resolve_cluster_metadata(context)
                self._print_cluster_banner(context, py_version, dep_result)

            except (ConfigException, ApiException, OSError) as e:
                errors.append(f"Kubernetes connection failed: {e}")
                return StepResult(
                    step_number=self.number,
                    step_name=self.name,
                    success=False,
                    message="Cluster connectivity check failed",
                    errors=errors,
                )
        else:
            # Dry-run: still print a minimal banner
            self._print_dry_run_banner(context, py_version, dep_result)

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=(
                f"All checks passed. "
                f"Tools: {', '.join(dep_result.available)}. "
                f"Python: {py_version}. "
                f"Platform: {context.platform_type}"
            ),
            context={
                "python_version": py_version,
                "available_tools": dep_result.available,
                "missing_optional": dep_result.missing_optional,
                "is_openshift": context.is_openshift,
                "is_kind": context.is_kind,
                "is_minikube": context.is_minikube,
                "platform_type": context.platform_type,
                "cluster_name": context.cluster_name,
                "cluster_server": context.cluster_server,
            },
        )

    # ------------------------------------------------------------------
    # Platform detection
    # ------------------------------------------------------------------

    def _detect_platform(self, context: ExecutionContext) -> None:
        """Detect the cluster platform beyond just OpenShift.

        Checks for Kind and Minikube by inspecting kube-system pods.
        """
        if context.is_openshift:
            # Already identified — skip further checks
            return

        kube_bin = "oc" if shutil.which("oc") else "kubectl"
        kubeconfig_args = ""
        if context.kubeconfig:
            kubeconfig_args = f"--kubeconfig {context.kubeconfig}"

        # Kind: look for kindnet in kube-system
        try:
            result = subprocess.run(
                f"{kube_bin} {kubeconfig_args} get pods -n kube-system "
                f"-o jsonpath='{{.items[*].metadata.name}}'",
                shell=True, capture_output=True, text=True,
                check=False, executable="/bin/bash",
            )
            pod_names = result.stdout if result.returncode == 0 else ""

            if "kindnet" in pod_names or "kind-cluster" in pod_names:
                context.is_kind = True
                return

            if "etcd-minikube" in pod_names or "minikube" in pod_names:
                context.is_minikube = True
                return
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Cluster metadata & banner
    # ------------------------------------------------------------------

    def _resolve_cluster_metadata(self, context: ExecutionContext) -> None:
        """
        Populate context.cluster_server, context.cluster_name, and
        context.context_name from the active kubeconfig.
        """
        kube_bin = "oc" if context.is_openshift else "kubectl"
        kubeconfig = context.kubeconfig

        cmd_parts = [kube_bin]
        if kubeconfig:
            cmd_parts.extend(["--kubeconfig", kubeconfig])
        cmd_parts.extend(["config", "view", "-o", "json"])

        try:
            result = subprocess.run(
                " ".join(cmd_parts),
                shell=True, capture_output=True, text=True,
                check=False, executable="/bin/bash",
            )
            if result.returncode != 0:
                return

            data = json.loads(result.stdout)
            context.context_name = data.get("current-context", "")

            # Find the cluster for this context
            for ctx in data.get("contexts", []):
                if ctx.get("name") == context.context_name:
                    cluster_ref = ctx.get("context", {}).get("cluster", "")
                    # Resolve namespace from context if not already set
                    ctx_namespace = ctx.get("context", {}).get("namespace")
                    if ctx_namespace and not context.namespace:
                        pass  # namespace comes from rendered plan, not kube context

                    for cluster in data.get("clusters", []):
                        if cluster.get("name") == cluster_ref:
                            server = cluster.get("cluster", {}).get("server", "")
                            context.cluster_server = server
                            # Extract hostname
                            from urllib.parse import urlparse
                            parsed = urlparse(server)
                            context.cluster_name = parsed.hostname
                            break
                    break
        except (json.JSONDecodeError, OSError):
            pass

    def _print_cluster_banner(
        self,
        context: ExecutionContext,
        py_version: str,
        dep_result,
    ) -> None:
        """Print a cluster connection summary banner."""
        log = context.logger
        if not log:
            return

        from llmdbenchmark import __version__

        server = context.cluster_server or "unknown"
        ctx_name = context.context_name or "unknown"
        platform = context.platform_type
        namespace = context.namespace or "(from plan)"
        stacks = len(context.rendered_stacks)

        # Print banner at top level (no indent) for prominence
        # Inner width: 60 chars between ║ borders
        W = 60
        saved_indent = getattr(log, "_indent_level", 0)
        log.set_indent(0)

        log.line_break()
        log.log_info("╔" + "═" * W + "╗")
        log.log_info(f"║  llm-d-benchmark v{__version__:<{W - 19}}║")
        log.log_info(f"║  Server:    {server:<{W - 13}}║")
        log.log_info(f"║  Platform:  {platform:<{W - 13}}║")
        log.log_info(f"║  Context:   {ctx_name[:W - 13]:<{W - 13}}║")
        log.log_info(f"║  Namespace: {namespace:<{W - 13}}║")
        log.log_info(f"║  Stacks:    {stacks:<{W - 13}}║")
        log.log_info(f"║  Python:    {py_version:<{W - 13}}║")
        log.log_info("╚" + "═" * W + "╝")
        log.line_break()

        log.set_indent(saved_indent)

    def _print_dry_run_banner(
        self,
        context: ExecutionContext,
        py_version: str,
        dep_result,
    ) -> None:
        """Print a minimal banner for dry-run mode (no cluster connection)."""
        log = context.logger
        if not log:
            return

        from llmdbenchmark import __version__

        namespace = context.namespace or "(from plan)"
        stacks = len(context.rendered_stacks)

        # Print banner at top level (no indent) for prominence
        # Inner width: 60 chars between ║ borders
        W = 60
        saved_indent = getattr(log, "_indent_level", 0)
        log.set_indent(0)

        log.line_break()
        log.log_info("╔" + "═" * W + "╗")
        log.log_info(f"║  llm-d-benchmark v{__version__:<{W - 19}}║")
        log.log_info(f"║  Mode:      {'DRY RUN (no cluster connection)':<{W - 13}}║")
        log.log_info(f"║  Namespace: {namespace:<{W - 13}}║")
        log.log_info(f"║  Stacks:    {stacks:<{W - 13}}║")
        log.log_info(f"║  Python:    {py_version:<{W - 13}}║")
        log.log_info("╚" + "═" * W + "╝")
        log.line_break()

        log.set_indent(saved_indent)

    # ------------------------------------------------------------------
    # Kubeconfig context.ctx management
    # ------------------------------------------------------------------

    def _store_kubeconfig(self, context: ExecutionContext) -> None:
        """Create a self-contained ``context.ctx`` in the workspace environment
        directory.

        The resulting file is used as ``--kubeconfig`` for all kubectl, helm,
        and helmfile commands in subsequent steps.

        Priority:
        1. Explicit ``--kubeconfig`` CLI path → copy to context.ctx
        2. Named config file ``~/.kube/config-{cluster_name}`` → copy
        3. Extract from current context → ``kubectl config view --minify …``
        4. ``oc login`` when ``--cluster-url`` / ``--cluster-token`` are given
        """
        env_dir = context.environment_dir()
        context_file = env_dir / "context.ctx"
        log = context.logger

        # --- Remove stale context.ctx ---
        if context_file.exists():
            if self._is_context_stale(context_file, context):
                if log:
                    log.log_warning(
                        f"Removing stale context.ctx (points to a different cluster)"
                    )
                context_file.unlink()

        # --- Create context.ctx ---
        if not context_file.exists():
            created = False

            # Method 1: Explicit kubeconfig from CLI
            if context.kubeconfig and Path(context.kubeconfig).is_file():
                shutil.copy2(context.kubeconfig, context_file)
                created = True
                if log:
                    log.log_info(
                        f"📄 Stored kubeconfig from {context.kubeconfig}"
                    )

            # Method 2: Named config file ~/.kube/config-{cluster_name}
            if not created:
                cluster_name = self._get_cluster_name(context)
                if cluster_name:
                    named_config = Path.home() / ".kube" / f"config-{cluster_name}"
                    if named_config.is_file():
                        shutil.copy2(named_config, context_file)
                        created = True
                        if log:
                            log.log_info(
                                f"📄 Stored kubeconfig from {named_config}"
                            )

            # Method 3: Extract from current context
            if not created:
                extracted = self._extract_current_context(context_file)
                if extracted:
                    created = True
                    if log:
                        log.log_info(
                            "📄 Extracted current kubeconfig context → context.ctx"
                        )

            # Method 4: oc login with URL + token (OpenShift)
            if not created and context.cluster_url and context.cluster_token:
                self._oc_login_and_store(context, context_file)
                created = context_file.exists()
                if created and log:
                    log.log_info(
                        f"📄 Stored kubeconfig via oc login → context.ctx"
                    )

            if not created and log:
                log.log_warning(
                    "Could not create context.ctx — "
                    "subsequent steps will use default kubeconfig"
                )

        # --- Point all subsequent commands at context.ctx ---
        if context_file.exists():
            context.kubeconfig = str(context_file)
            if log:
                log.log_info(
                    f"🔑 Using kubeconfig: {context_file}"
                )

    def _is_context_stale(
        self, context_file: Path, context: ExecutionContext
    ) -> bool:
        """
        Check if a stored context.ctx points to a different cluster than
        the current session.  Returns True if stale.
        """
        kube_bin = "oc" if context.is_openshift else "kubectl"
        try:
            # Get server from stored context.ctx
            stored_server = self._get_server_from_kubeconfig(
                kube_bin, str(context_file)
            )
            if not stored_server:
                return True  # Can't determine — treat as stale

            # Get server from current (active) context
            current_server = self._get_server_from_kubeconfig(kube_bin)
            if not current_server:
                return False  # Can't determine current — keep stored

            return stored_server != current_server
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    def _get_server_from_kubeconfig(
        self, kube_bin: str, kubeconfig: str | None = None
    ) -> str | None:
        """Extract the API server URL from a kubeconfig file or current config."""
        cmd_parts = [kube_bin]
        if kubeconfig:
            cmd_parts.extend(["--kubeconfig", kubeconfig])
        cmd_parts.extend(["config", "view", "-o", "json"])

        try:
            result = subprocess.run(
                " ".join(cmd_parts),
                shell=True, capture_output=True, text=True,
                check=False, executable="/bin/bash",
            )
            if result.returncode != 0:
                return None

            data = json.loads(result.stdout)
            current_ctx = data.get("current-context", "")
            if not current_ctx:
                return None

            # Find the cluster name for this context
            for ctx in data.get("contexts", []):
                if ctx.get("name") == current_ctx:
                    cluster_name = ctx.get("context", {}).get("cluster", "")
                    # Find the server for this cluster
                    for cluster in data.get("clusters", []):
                        if cluster.get("name") == cluster_name:
                            return cluster.get("cluster", {}).get("server")
            return None
        except (json.JSONDecodeError, OSError):
            return None

    def _get_cluster_name(self, context: ExecutionContext) -> str | None:
        """
        Derive a short cluster name from the current server URL.

        Extracts the hostname from the API server URL, e.g.
        ``https://api.my-cluster.example.com:6443`` → ``api.my-cluster.example.com``.
        """
        kube_bin = "oc" if context.is_openshift else "kubectl"
        server = self._get_server_from_kubeconfig(kube_bin)
        if not server:
            return None
        # https://api.cluster.example.com:6443 → api.cluster.example.com
        from urllib.parse import urlparse
        parsed = urlparse(server)
        return parsed.hostname

    def _extract_current_context(self, target_file: Path) -> bool:
        """
        Extract the current kubeconfig context into a self-contained file
        using ``kubectl config view --minify --flatten --raw``.
        """
        for kube_bin in ("oc", "kubectl"):
            if not shutil.which(kube_bin):
                continue
            try:
                result = subprocess.run(
                    f"{kube_bin} config view --minify --flatten --raw",
                    shell=True, capture_output=True, text=True,
                    check=False, executable="/bin/bash",
                )
                if result.returncode == 0 and result.stdout.strip():
                    target_file.write_text(result.stdout)
                    return True
            except OSError:
                continue
        return False

    def _oc_login_and_store(
        self, context: ExecutionContext, target_file: Path
    ) -> None:
        """
        Login via ``oc login --token=… --server=…`` and then extract
        the resulting kubeconfig to ``target_file``.
        """
        if not shutil.which("oc"):
            return

        server = context.cluster_url
        if not server.startswith("http"):
            server = f"https://{server}"
        if ":6443" not in server and ":443" not in server:
            server = f"{server}:6443"

        try:
            subprocess.run(
                f'oc login --token="{context.cluster_token}" '
                f'--server="{server}" --insecure-skip-tls-verify=true',
                shell=True, capture_output=True, text=True,
                check=False, executable="/bin/bash",
            )
            # Now extract the resulting config
            self._extract_current_context(target_file)
        except OSError:
            pass
