"""
llmdbenchmark.executor.step_executor

Phase-agnostic step orchestrator that discovers, filters, and executes
steps sequentially (global steps) or in parallel (per-stack steps).
Works for standup, run, and teardown phases.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from llmdbenchmark.executor.step import (
    Step,
    StepResult,
    StackExecutionResult,
    ExecutionResult,
)
from llmdbenchmark.executor.context import ExecutionContext


class StepExecutor:
    """
    Execute a list of steps, handling global vs per-stack dispatch
    and optional parallelism across rendered stacks.

    Args:
        steps: Ordered list of Step instances to run.
        context: Shared ExecutionContext with paths, flags, and state.
        logger: Logger instance for structured logging.
        max_parallel_stacks: Maximum number of stacks to deploy in parallel.
    """

    def __init__(
        self,
        steps: list[Step],
        context: ExecutionContext,
        logger,
        max_parallel_stacks: int = 4,
    ):
        self.steps = sorted(steps, key=lambda s: s.number)
        self.context = context
        self.logger = logger
        self.max_parallel_stacks = max_parallel_stacks

    def parse_step_list(self, step_spec: str) -> list[int]:
        """
        Parse a step specification string into a sorted list of step numbers.

        Supports comma-separated values and ranges:
            "0,1,5"   -> [0, 1, 5]
            "1-7"     -> [1, 2, 3, 4, 5, 6, 7]
            "0,3-5,9" -> [0, 3, 4, 5, 9]

        Args:
            step_spec: Step specification string.

        Returns:
            Sorted, deduplicated list of step numbers.
        """
        result = set()
        for part in step_spec.split(","):
            part = part.strip()
            if not part:
                continue
            range_match = re.match(r"^(\d+)-(\d+)$", part)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2))
                result.update(range(start, end + 1))
            else:
                result.add(int(part))
        return sorted(result)

    def execute(self, step_spec: str | None = None) -> ExecutionResult:
        """
        Execute all (or filtered) steps for the current phase.

        Global steps (per_stack=False) run sequentially first.
        Per-stack steps (per_stack=True) then run in parallel across stacks.

        Args:
            step_spec: Optional step filter (e.g., "0,1,5" or "1-7").
                       If None, all steps are executed.

        Returns:
            ExecutionResult with global and per-stack results.
        """
        result = ExecutionResult(phase=self.context.current_phase)
        allowed_numbers = set(self.parse_step_list(step_spec)) if step_spec else None

        global_steps, per_stack_steps = self._partition_steps(allowed_numbers)

        # Phase 1: Execute global steps sequentially
        abort = self._execute_global_steps(global_steps, result)
        if abort:
            return result

        # Phase 2: Execute per-stack steps
        self._execute_per_stack_steps(per_stack_steps, result)

        return result

    def _partition_steps(
        self, allowed_numbers: set[int] | None
    ) -> tuple[list[Step], list[Step]]:
        """Split steps into global and per-stack lists, filtered by allowed numbers."""
        global_steps = []
        per_stack_steps = []
        for step in self.steps:
            if allowed_numbers is not None and step.number not in allowed_numbers:
                continue
            if step.per_stack:
                per_stack_steps.append(step)
            else:
                global_steps.append(step)
        return global_steps, per_stack_steps

    def _execute_global_steps(
        self, global_steps: list[Step], result: ExecutionResult
    ) -> bool:
        """Execute global steps sequentially. Returns True if execution should abort."""
        self.logger.line_break()
        self.logger.log_info(
            f"📋 Executing {len(global_steps)} global step(s)...",
        )
        self.logger.line_break()

        for step in global_steps:
            if step.should_skip(self.context):
                self.logger.log_info(
                    f"⏭️  [{step.number:02d}] Skipping: {step.name} ({step.description})",
                )
                result.global_results.append(
                    StepResult(
                        step_number=step.number,
                        step_name=step.name,
                        success=True,
                        message="Skipped",
                    )
                )
                continue

            self.logger.log_info(
                f"▶️  [{step.number:02d}] {step.description}",
            )

            self.logger.set_indent(1)
            step_result = self._safe_execute_step(step, stack_path=None)
            self.logger.set_indent(0)
            result.global_results.append(step_result)

            if step_result.has_errors:
                self.logger.log_error(f"❌ [{step.number:02d}] FAILED: {step_result}")
                result.errors.append(
                    f"Global step {step.number:02d} ({step.name}) failed"
                )
                return True

            self.logger.log_info(
                f"✅ [{step.number:02d}] Completed: {step.name}",
            )

        return False

    def _execute_per_stack_steps(
        self, per_stack_steps: list[Step], result: ExecutionResult
    ) -> None:
        """Execute per-stack steps, in parallel if multiple stacks exist."""
        if not per_stack_steps:
            return

        stacks = self.context.rendered_stacks
        if not stacks:
            self.logger.log_warning(
                "No rendered stacks found. Skipping per-stack steps."
            )
            return

        self.logger.line_break()
        self.logger.log_info(
            f"📋 Executing {len(per_stack_steps)} per-stack step(s) across "
            f"{len(stacks)} stack(s) (max parallel: {self.max_parallel_stacks})...",
        )
        self.logger.line_break()

        if len(stacks) == 1:
            stack_result = self._execute_stack(stacks[0], per_stack_steps)
            result.stack_results.append(stack_result)
        else:
            self._execute_stacks_parallel(stacks, per_stack_steps, result)

    def _execute_stacks_parallel(
        self, stacks: list[Path], steps: list[Step], result: ExecutionResult
    ) -> None:
        """Execute per-stack steps across multiple stacks in parallel."""
        with ThreadPoolExecutor(
            max_workers=min(self.max_parallel_stacks, len(stacks))
        ) as pool:
            futures = {
                pool.submit(self._execute_stack, stack_path, steps): stack_path
                for stack_path in stacks
            }
            for future in as_completed(futures):
                stack_path = futures[future]
                try:
                    stack_result = future.result()
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    stack_name = stack_path.name
                    self.logger.log_error(
                        f"Stack '{stack_name}' raised exception: {exc}"
                    )
                    stack_result = StackExecutionResult(
                        stack_name=stack_name,
                        stack_path=stack_path,
                        step_results=[
                            StepResult(
                                step_number=-1,
                                step_name="exception",
                                success=False,
                                message=str(exc),
                            )
                        ],
                    )
                result.stack_results.append(stack_result)

    def _execute_stack(
        self,
        stack_path: Path,
        steps: list[Step],
    ) -> StackExecutionResult:
        """
        Execute per-stack steps sequentially for a single stack.

        Args:
            stack_path: Path to the rendered YAML directory for this stack.
            steps: List of per-stack Step instances to execute.

        Returns:
            StackExecutionResult with results for this stack.
        """
        stack_name = stack_path.name
        stack_result = StackExecutionResult(
            stack_name=stack_name, stack_path=stack_path
        )

        self.logger.log_info(
            f"🚀 Stack '{stack_name}': starting {len(steps)} step(s)...",
        )

        for step in steps:
            if step.should_skip(self.context):
                self.logger.log_info(
                    f"Stack '{stack_name}' [{step.number:02d}] Skipping: {step.name}"
                )
                stack_result.step_results.append(
                    StepResult(
                        step_number=step.number,
                        step_name=step.name,
                        success=True,
                        message="Skipped",
                        stack_name=stack_name,
                    )
                )
                continue

            self.logger.log_info(
                f"▶️  Stack '{stack_name}' [{step.number:02d}] {step.description}"
            )

            self.logger.set_indent(1)
            step_result = self._safe_execute_step(step, stack_path=stack_path)
            self.logger.set_indent(0)
            step_result.stack_name = stack_name
            stack_result.step_results.append(step_result)

            if step_result.has_errors:
                self.logger.log_error(
                    f"❌ Stack '{stack_name}' [{step.number:02d}] FAILED: {step_result}"
                )
                break

            self.logger.log_info(
                f"✅ Stack '{stack_name}' [{step.number:02d}] Completed: {step.name}",
            )

        return stack_result

    def _safe_execute_step(
        self, step: Step, stack_path: Path | None
    ) -> StepResult:
        """
        Execute a step with exception handling.

        Args:
            step: The Step instance to execute.
            stack_path: Optional stack path for per-stack steps.

        Returns:
            StepResult, always (never raises).
        """
        try:
            return step.execute(self.context, stack_path=stack_path)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.logger.log_error(
                f"Step {step.number:02d} ({step.name}) raised exception: {exc}"
            )
            return StepResult(
                step_number=step.number,
                step_name=step.name,
                success=False,
                message=f"Exception: {exc}",
                errors=[str(exc)],
            )
