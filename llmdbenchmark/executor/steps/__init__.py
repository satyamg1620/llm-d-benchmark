"""
llmdbenchmark.executor.steps

Step registry for all execution phases.

Provides factory functions that return ordered lists of Step instances
for each phase, used by StepExecutor.
"""

from llmdbenchmark.executor.step import Step, Phase

from llmdbenchmark.executor.steps.step_00_ensure_infra import EnsureInfraStep
from llmdbenchmark.executor.steps.step_01_ensure_conda import EnsureCondaStep
from llmdbenchmark.executor.steps.step_02_admin_prerequisites import AdminPrerequisitesStep
from llmdbenchmark.executor.steps.step_03_workload_monitoring import WorkloadMonitoringStep
from llmdbenchmark.executor.steps.step_04_model_namespace import ModelNamespaceStep
from llmdbenchmark.executor.steps.step_05_harness_namespace import HarnessNamespaceStep
from llmdbenchmark.executor.steps.step_06_standalone_deploy import StandaloneDeployStep
from llmdbenchmark.executor.steps.step_07_deploy_setup import DeploySetupStep
from llmdbenchmark.executor.steps.step_08_deploy_gaie import DeployGaieStep
from llmdbenchmark.executor.steps.step_09_deploy_modelservice import DeployModelserviceStep
from llmdbenchmark.executor.steps.step_10_smoketest import SmoketestStep


def get_standup_steps() -> list[Step]:
    """Return all standup-phase steps in execution order."""
    return [
        EnsureInfraStep(),
        EnsureCondaStep(),
        AdminPrerequisitesStep(),
        WorkloadMonitoringStep(),
        ModelNamespaceStep(),
        HarnessNamespaceStep(),
        StandaloneDeployStep(),
        DeploySetupStep(),
        DeployGaieStep(),
        DeployModelserviceStep(),
        SmoketestStep(),
    ]


def get_steps_for_phase(phase: Phase) -> list[Step]:
    """Return all steps for a given phase."""
    if phase == Phase.STANDUP:
        return get_standup_steps()
    # Future: Phase.RUN and Phase.TEARDOWN
    return []
