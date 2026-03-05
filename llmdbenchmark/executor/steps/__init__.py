"""
llmdbenchmark.executor.steps

Backward-compatibility shim.

Step registries have moved to their phase-specific packages:
- ``llmdbenchmark.standup.steps``   → ``get_standup_steps()``
- ``llmdbenchmark.teardown.steps``  → ``get_teardown_steps()``

This module re-exports the factory functions so existing code continues
to work.  New code should import directly from the phase packages.
"""

from llmdbenchmark.executor.step import Phase, Step
from llmdbenchmark.standup.steps import get_standup_steps
from llmdbenchmark.teardown.steps import get_teardown_steps


def get_steps_for_phase(phase: Phase) -> list[Step]:
    """Return all steps for a given phase."""
    if phase == Phase.STANDUP:
        return get_standup_steps()
    if phase == Phase.TEARDOWN:
        return get_teardown_steps()
    # Future: Phase.RUN
    return []
