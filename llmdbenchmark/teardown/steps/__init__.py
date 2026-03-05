"""
llmdbenchmark.teardown.steps

Step registry for the teardown phase.

Provides ``get_teardown_steps()`` which returns an ordered list of Step
instances used by ``StepExecutor`` to tear down a previously deployed
llm-d stack.
"""

from llmdbenchmark.executor.step import Step

from llmdbenchmark.teardown.steps.step_00_preflight import TeardownPreflightStep
from llmdbenchmark.teardown.steps.step_01_uninstall_helm import UninstallHelmStep
from llmdbenchmark.teardown.steps.step_02_clean_harness import CleanHarnessStep
from llmdbenchmark.teardown.steps.step_03_delete_resources import DeleteResourcesStep
from llmdbenchmark.teardown.steps.step_04_clean_cluster_roles import CleanClusterRolesStep


def get_teardown_steps() -> list[Step]:
    """Return all teardown-phase steps in execution order."""
    return [
        TeardownPreflightStep(),
        UninstallHelmStep(),
        CleanHarnessStep(),
        DeleteResourcesStep(),
        CleanClusterRolesStep(),
    ]
