"""
llmdbenchmark.executor.step

Defines the Step abstract base class, Phase enum, StepResult, and ExecutionResult
dataclasses used by the step executor framework.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class Phase(Enum):
    """Execution phases corresponding to the benchmark lifecycle."""

    STANDUP = "standup"
    RUN = "run"
    TEARDOWN = "teardown"


@dataclass
class StepResult:
    """Result of executing a single step."""

    step_number: int
    step_name: str
    success: bool
    message: str = ""
    errors: list[str] = field(default_factory=list)
    stack_name: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        """Return True if this step failed or has recorded errors."""
        return not self.success or len(self.errors) > 0

    def __str__(self) -> str:
        status = "OK" if self.success else "FAILED"
        prefix = f"[{self.step_number:02d}] {self.step_name}: {status}"
        if self.stack_name:
            prefix = f"[{self.step_number:02d}] {self.step_name} ({self.stack_name}): {status}"
        if self.message:
            return f"{prefix} - {self.message}"
        return prefix


@dataclass
class StackExecutionResult:
    """Result of executing all per-stack steps for a single stack."""

    stack_name: str
    stack_path: Path
    step_results: list[StepResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Return True if any step in this stack failed."""
        return any(r.has_errors for r in self.step_results)

    @property
    def failed_steps(self) -> list[StepResult]:
        """Return the list of steps that failed."""
        return [r for r in self.step_results if r.has_errors]


@dataclass
class ExecutionResult:
    """Aggregate result of a full phase execution."""

    phase: Phase
    global_results: list[StepResult] = field(default_factory=list)
    stack_results: list[StackExecutionResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Return True if any global or per-stack step failed."""
        if self.errors:
            return True
        if any(r.has_errors for r in self.global_results):
            return True
        return any(sr.has_errors for sr in self.stack_results)

    def summary(self) -> str:
        """Return a human-readable summary of the execution."""
        lines = [f"Phase: {self.phase.value}"]

        if self.errors:
            lines.append(f"Global errors: {len(self.errors)}")
            for err in self.errors:
                lines.append(f"  - {err}")

        failed_global = [r for r in self.global_results if r.has_errors]
        if failed_global:
            lines.append(f"Failed global steps: {len(failed_global)}")
            for r in failed_global:
                lines.append(f"  - {r}")

        for sr in self.stack_results:
            if sr.has_errors:
                lines.append(f"Stack '{sr.stack_name}' failures:")
                for r in sr.failed_steps:
                    lines.append(f"  - {r}")

        if not self.has_errors:
            total = len(self.global_results) + sum(
                len(sr.step_results) for sr in self.stack_results
            )
            lines.append(f"All {total} step(s) completed successfully.")

        return "\n".join(lines)


class Step(ABC):
    """
    Abstract base class for all execution steps.

    Each step represents a discrete unit of work in the standup/run/teardown
    pipeline. Steps declare their phase and whether they operate globally
    (once for all stacks) or per-stack (once per rendered stack, parallelizable).

    Attributes:
        number: Step number (e.g., 0, 1, 2, ...) used for ordering and filtering.
        name: Short identifier (e.g., "ensure_infra").
        description: Human-readable description of what this step does.
        phase: Which lifecycle phase this step belongs to.
        per_stack: If True, this step runs once per stack and receives a stack_path.
                   If False, this step runs once globally.
    """

    number: int
    name: str
    description: str
    phase: Phase
    per_stack: bool

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        number: int,
        name: str,
        description: str,
        phase: Phase,
        per_stack: bool = False,
    ):
        self.number = number
        self.name = name
        self.description = description
        self.phase = phase
        self.per_stack = per_stack

    @abstractmethod
    def execute(self, context: "ExecutionContext", stack_path: Path | None = None) -> StepResult:
        """
        Execute this step.

        Args:
            context: Shared execution context with paths, flags, and state.
            stack_path: Path to the rendered YAML directory for this stack.
                        Only provided when per_stack=True.

        Returns:
            StepResult indicating success/failure with details.
        """

    def should_skip(self, context: "ExecutionContext") -> bool:  # pylint: disable=unused-argument
        """
        Determine whether this step should be skipped based on context.

        Override in subclasses to implement conditional skip logic.
        For example, standalone-only steps skip when method is modelservice.

        Args:
            context: Shared execution context.

        Returns:
            True if this step should be skipped.
        """
        return False

    # ------------------------------------------------------------------
    # Shared config/YAML helpers available to all steps
    # ------------------------------------------------------------------

    def _load_plan_config(self, context: "ExecutionContext") -> dict | None:
        """Load the merged config.yaml from the first rendered stack."""
        for stack_path in context.rendered_stacks:
            config_file = stack_path / "config.yaml"
            if config_file.exists():
                with open(config_file, encoding="utf-8") as f:
                    return yaml.safe_load(f)
        return None

    def _load_stack_config(self, stack_path: Path) -> dict:
        """Load config.yaml from a specific stack directory."""
        config_file = stack_path / "config.yaml"
        if config_file.exists():
            with open(config_file, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    def _find_rendered_yaml(
        self, context: "ExecutionContext", prefix: str
    ) -> Path | None:
        """Find a rendered YAML file by prefix across all stacks."""
        for stack_path in context.rendered_stacks:
            for yaml_file in sorted(stack_path.glob(f"{prefix}*")):
                return yaml_file
        return None

    def _find_yaml(self, stack_path: Path, prefix: str) -> Path | None:
        """Find a YAML file by prefix in a stack directory."""
        for yaml_file in sorted(stack_path.glob(f"{prefix}*")):
            return yaml_file
        return None

    def __repr__(self) -> str:
        return (
            f"Step({self.number:02d}, {self.name!r}, "
            f"phase={self.phase.value}, per_stack={self.per_stack})"
        )
