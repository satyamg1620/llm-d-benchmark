"""
Step 03: Workload Monitoring and Resource Discovery

Discovers cluster node resources (GPU accelerators, network interfaces)
and optionally configures user workload monitoring on OpenShift clusters.

This step:
1. Discovers node resources (GPU types, RDMA/IB network adapters)
2. Auto-detects accelerator resource names for pod scheduling
3. Auto-detects network resources for multi-node communication
4. Applies monitoring configuration on OpenShift (if admin)
"""

import json
from pathlib import Path

import yaml

from llmdbenchmark.executor.step import Step, StepResult, Phase
from llmdbenchmark.executor.context import ExecutionContext
from llmdbenchmark.executor.command import CommandExecutor


class WorkloadMonitoringStep(Step):
    """Discover node resources and configure user workload monitoring."""

    def __init__(self):
        super().__init__(
            number=3,
            name="workload_monitoring",
            description="Discover node resources and configure monitoring",
            phase=Phase.STANDUP,
            per_stack=False,
        )

    def should_skip(self, context: ExecutionContext) -> bool:
        # Only skip if non-admin — resource discovery is useful on all platforms
        return context.non_admin

    def execute(
        self, context: ExecutionContext, stack_path: Path | None = None
    ) -> StepResult:
        cmd = CommandExecutor(
            work_dir=context.workspace,
            dry_run=context.dry_run,
            verbose=context.verbose,
            logger=context.logger,
            kubeconfig=context.kubeconfig,
            openshift=context.is_openshift,
        )

        errors = []

        # 1. Discover node resources (accelerators, network)
        self._discover_node_resources(cmd, context, errors)

        # 2. Auto-detect accelerator type
        self._check_accelerator(cmd, context, errors)

        # 3. Auto-detect network resources
        self._check_network(cmd, context, errors)

        # 4. Capacity planner sanity check
        self._capacity_planner_sanity_check(cmd, context, errors)

        # 5. Apply monitoring configuration (OpenShift only)
        if context.is_openshift:
            self._apply_monitoring(cmd, context, errors)

        if errors:
            return StepResult(
                step_number=self.number,
                step_name=self.name,
                success=False,
                message="Resource discovery / monitoring had errors",
                errors=errors,
            )

        return StepResult(
            step_number=self.number,
            step_name=self.name,
            success=True,
            message="Node resources discovered and monitoring configured",
        )

    def _discover_node_resources(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Scan node capacities to discover GPU and network resources."""
        if context.dry_run:
            context.logger.log_info(
                "[DRY RUN] Would discover node resources"
            )
            return

        result = cmd.kubectl(
            "get", "nodes",
            "-o", "jsonpath={.items[*].status.capacity}",
        )
        if not result.success:
            context.logger.log_warning(
                "Could not query node capacities — continuing without "
                "resource discovery"
            )
            return

        # Parse the concatenated JSON objects
        raw = result.stdout.strip()
        if not raw:
            return

        # Look for accelerator resources in node capacities
        accelerator_resources = set()
        network_resources = set()

        # kubectl returns space-separated JSON objects; try to parse
        # Use a simple heuristic: look for known resource key patterns
        for known_accel in [
            "nvidia.com/gpu",
            "amd.com/gpu",
            "habana.ai/gaudi",
            "google.com/tpu",
            "intel.com/gpu",
        ]:
            if known_accel in raw:
                accelerator_resources.add(known_accel)

        for known_net in [
            "rdma/rdma_shared_device_a",
            "rdma/hca_shared_devices_a",
            "nvidia.com/hostdev",
        ]:
            if known_net in raw:
                network_resources.add(known_net)

        if accelerator_resources:
            # Pick the first detected accelerator
            accel = sorted(accelerator_resources)[0]
            context.accelerator_resource = accel
            context.logger.log_info(
                f"🔍 Detected accelerator resource: {accel}"
            )
        else:
            context.logger.log_info(
                "ℹ️  No GPU accelerator resources detected on nodes"
            )

        if network_resources:
            net = sorted(network_resources)[0]
            context.network_resource = net
            context.logger.log_info(
                f"🔍 Detected network resource: {net}"
            )

    def _check_accelerator(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Validate and log the detected accelerator resource."""
        if context.dry_run:
            return

        if not context.accelerator_resource:
            context.logger.log_info(
                "ℹ️  No accelerator resource set — "
                "pods will not request GPU resources"
            )
            return

        # Verify at least one node has this resource with count > 0
        result = cmd.kubectl(
            "get", "nodes",
            "-o", f"jsonpath={{.items[*].status.capacity.{context.accelerator_resource}}}",
        )
        if result.success and result.stdout.strip():
            counts = result.stdout.strip().split()
            total = sum(int(c) for c in counts if c.isdigit())
            context.logger.log_info(
                f"✅ Accelerator {context.accelerator_resource}: "
                f"{total} total across {len(counts)} node(s)"
            )
        else:
            context.logger.log_warning(
                f"⚠️  Accelerator {context.accelerator_resource} "
                "declared but no capacity found on nodes"
            )

    def _check_network(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Check for RDMA/InfiniBand network resources."""
        if context.dry_run:
            return

        if not context.network_resource:
            context.logger.log_info(
                "ℹ️  No RDMA/IB network resource detected"
            )
            return

        context.logger.log_info(
            f"✅ Network resource available: {context.network_resource}"
        )

    def _capacity_planner_sanity_check(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Validate vLLM resource requests against available cluster capacity."""
        if context.dry_run:
            return

        plan_config = self._load_plan_config(context)
        if not plan_config:
            return

        vllm_config = plan_config.get("vllmCommon", {})
        tp = int(vllm_config.get("tensorParallel", 1))
        pp = int(vllm_config.get("pipelineParallel", 1))
        replicas = int(vllm_config.get("replicas", 1))
        accelerator_nr = int(vllm_config.get("acceleratorNr", tp * pp))

        # Check: requested GPUs per pod vs available
        if context.accelerator_resource and accelerator_nr > 0:
            # Count total GPUs across nodes
            result = cmd.kubectl(
                "get", "nodes",
                "-o", f"jsonpath={{.items[*].status.capacity.{context.accelerator_resource}}}",
            )
            if result.success and result.stdout.strip():
                counts = result.stdout.strip().split()
                total_gpus = sum(int(c) for c in counts if c.isdigit())
                needed = accelerator_nr * replicas

                if needed > total_gpus:
                    context.logger.log_warning(
                        f"⚠️  Capacity check: requesting {needed} GPUs "
                        f"({accelerator_nr}/pod × {replicas} replicas) "
                        f"but only {total_gpus} available"
                    )

        # Check: max_model_len is a power of 2 or reasonable value
        max_model_len = vllm_config.get("maxModelLen")
        if max_model_len:
            max_model_len = int(max_model_len)
            if max_model_len < 128:
                context.logger.log_warning(
                    f"⚠️  Capacity check: maxModelLen={max_model_len} "
                    "seems too low (typical: 2048-32768)"
                )

        context.logger.log_info(
            f"✅ Capacity check: TP={tp}, PP={pp}, replicas={replicas}, "
            f"accelerators/pod={accelerator_nr}"
        )

    def _apply_monitoring(
        self, cmd: CommandExecutor, context: ExecutionContext, errors: list
    ):
        """Apply monitoring configuration on OpenShift clusters."""
        monitoring_yaml = self._find_rendered_yaml(
            context, "03_cluster-monitoring-config"
        )

        if not monitoring_yaml:
            context.logger.log_info(
                "No monitoring config found, skipping monitoring setup"
            )
            return

        result = cmd.kubectl("apply", "-f", str(monitoring_yaml))
        if not result.success:
            errors.append(
                f"Failed to apply monitoring configuration: {result.stderr}"
            )
        else:
            context.logger.log_info(
                "✅ User workload monitoring configured"
            )
