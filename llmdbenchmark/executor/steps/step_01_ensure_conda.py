"""
Step 01: Ensure Local Conda Environment

Sets up a local conda/miniforge environment for running analysis tools.
This step is optional and can be skipped if conda is already configured
or if analysis will be done elsewhere (analyze_locally=True).

Per-stack step: runs once globally (not per-stack).
"""

import platform
import shutil
import subprocess
from pathlib import Path

import requests

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext


class EnsureCondaStep(Step):
    """Ensure a local conda/miniforge environment is available for analysis tools."""

    def __init__(self):
        super().__init__(
            number=1,
            name="ensure_conda",
            description="Ensure local conda environment is available",
            phase=Phase.STANDUP,
            per_stack=False,
        )

    def should_skip(self, context: ExecutionContext) -> bool:
        # Skip if we are only analyzing locally (no cluster interaction needed)
        return context.analyze_locally

    def execute(
        self, context: ExecutionContext, stack_path: Path | None = None
    ) -> StepResult:
        # Check if conda is already available
        conda_path = shutil.which("conda")
        if conda_path:
            context.logger.log_info(
                f"✅ Conda already available at {conda_path}"
            )
            # Even if conda exists, ensure the env is created
            env_result = self._ensure_conda_env(context, conda_path)
            if env_result:
                return env_result
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=True,
                message=f"Conda already available at {conda_path}",
            )

        if context.dry_run:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=True,
                message="[DRY RUN] Would install miniforge",
            )

        # Attempt to install miniforge
        system = platform.system().lower()

        if system == "darwin":
            success, msg = self._install_macos()
        elif system == "linux":
            success, msg = self._install_linux(context)
        else:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message=f"Unsupported platform: {system}",
                errors=[f"Cannot auto-install conda on {system}"],
            )

        if not success:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=success,
                message=msg,
                errors=[msg],
            )

        # After install, create the conda env
        conda_path = shutil.which("conda")
        if conda_path:
            env_result = self._ensure_conda_env(context, conda_path)
            if env_result:
                return env_result

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message=msg,
        )

    def _install_macos(self) -> tuple[bool, str]:
        """Install miniforge on macOS via Homebrew."""
        brew_path = shutil.which("brew")
        if not brew_path:
            return False, "Homebrew not found. Install miniforge manually."

        try:
            subprocess.run(
                ["brew", "install", "--cask", "miniforge"],
                capture_output=True,
                text=True,
                check=True,
            )
            return True, "Miniforge installed via Homebrew"
        except subprocess.CalledProcessError as e:
            return False, f"Homebrew install failed: {e.stderr[:200]}"

    def _install_linux(self, context: ExecutionContext) -> tuple[bool, str]:
        """Install miniforge on Linux via direct download."""
        try:
            arch = platform.machine()
            url = (
                f"https://github.com/conda-forge/miniforge/releases/latest/download/"
                f"Miniforge3-Linux-{arch}.sh"
            )
            installer_path = Path("/tmp/miniforge_installer.sh")

            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            installer_path.write_bytes(resp.content)
            installer_path.chmod(0o755)

            # Install to workspace/miniforge3 (matches bash behavior)
            install_dir = context.workspace / "miniforge3"
            subprocess.run(
                [str(installer_path), "-b", "-p", str(install_dir)],
                capture_output=True,
                text=True,
                check=True,
            )

            installer_path.unlink(missing_ok=True)

            # Add to PATH for subsequent steps
            conda_bin = install_dir / "bin"
            import os
            os.environ["PATH"] = f"{conda_bin}:{os.environ.get('PATH', '')}"

            return True, f"Miniforge installed to {install_dir}"

        except requests.RequestException as e:
            return False, f"Linux miniforge download failed: {e}"
        except subprocess.CalledProcessError as e:
            return False, f"Linux miniforge install failed: {e}"
        except OSError as e:
            return False, f"Linux miniforge install failed (filesystem error): {e}"

    def _ensure_conda_env(
        self, context: ExecutionContext, conda_path: str
    ) -> StepResult | None:
        """Create the conda environment if it doesn't exist and install requirements.

        Returns a StepResult on failure, None on success.
        """
        if context.dry_run:
            return None

        env_name = "inference-perf-env"  # default; could be made configurable

        # Check if env already exists
        try:
            result = subprocess.run(
                ["conda", "env", "list", "--json"],
                capture_output=True,
                text=True,
                check=True,
            )
            import json
            env_info = json.loads(result.stdout)
            existing_envs = [
                Path(e).name for e in env_info.get("envs", [])
            ]
            if env_name in existing_envs:
                context.logger.log_info(
                    f"✅ Conda environment '{env_name}' already exists"
                )
                return None
        except (subprocess.CalledProcessError, Exception):
            pass  # proceed to create

        # Create the conda environment
        context.logger.log_info(
            f"📦 Creating conda environment '{env_name}'..."
        )
        try:
            subprocess.run(
                ["conda", "create", "-n", env_name, "-y", "python=3.11"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message=f"Failed to create conda env: {e.stderr[:200]}",
                errors=[f"conda create failed: {e.stderr[:200]}"],
            )

        # Install requirements if a requirements.txt exists
        if context.base_dir:
            req_file = context.base_dir / "requirements.txt"
            if req_file.exists():
                context.logger.log_info(
                    "📦 Installing pip requirements into conda env..."
                )
                try:
                    subprocess.run(
                        [
                            "conda", "run", "-n", env_name,
                            "pip", "install", "-r", str(req_file),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                except subprocess.CalledProcessError as e:
                    context.logger.log_warning(
                        f"pip install requirements failed: {e.stderr[:200]}"
                    )

        return None
