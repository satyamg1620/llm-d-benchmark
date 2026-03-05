# llm-d-benchmark

Automated workflow for benchmarking LLM inference using the `llm-d` stack. Includes tools for deployment, experiment execution, data collection, and teardown across multiple environments and deployment styles.

## Main Goal

Provide a single source of automation for repeatable and reproducible experiments and performance evaluation on `llm-d`.

## Prerequisites

Please refer to the official [llm-d prerequisites](https://github.com/llm-d/llm-d/blob/main/README.md#pre-requisites) for the most up-to-date requirements.

### Administrative Requirements

Deploying the llm-d stack requires **cluster-level admin** privileges for configuring cluster-level resources. However, **namespace-level admin** users can run the tool as long as [Kubernetes infrastructure components](https://github.com/llm-d-incubation/llm-d-infra) are configured and the target namespace already exists. Use `--non-admin` to skip admin-only steps.

## Repository Setup

```bash
git clone https://github.com/llm-d/llm-d-benchmark.git
cd llm-d-benchmark
pip install -e .
```

## Quickstart

**Plan** the deployment (renders Jinja2 templates into YAML manifests):

```bash
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 plan
```

**Stand up** a full `llm-d` stack (plans + applies to cluster):

```bash
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 standup
```

Dry run (generates all YAML without touching the cluster):

```bash
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 --dry-run standup
```

**Tear down** a previously deployed stack:

```bash
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 teardown
```

Deep clean (remove all resources in both namespaces):

```bash
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 teardown --deep
```

See [config/README.md](config/README.md) for the full configuration reference, available specifications, and how to create your own.

## Architecture

The tool operates in three phases:

1. **Plan phase** -- Renders Jinja2 templates with scenario values into complete Kubernetes YAML manifests, Helm values files, and helmfile configurations.
2. **Standup phase** -- Executes a sequence of numbered steps that apply those rendered manifests to a Kubernetes cluster.
3. **Teardown phase** -- Reverses the standup by removing deployed resources, Helm releases, and cluster-scoped roles.

```text
specification.yaml.j2
        |
        v
   [Plan Phase]           Jinja2 + scenario values + defaults
        |
        v
  rendered stacks/        One directory per model stack with all YAMLs
        |
        v
  [Standup Phase]          Steps 00-10 executed sequentially / per-stack
        |
        v
  Running cluster          vLLM pods serving models, ready for benchmarks
        |
        v
  [Teardown Phase]         Steps 00-04 reverse the standup
        |
        v
  Clean cluster            Namespaces cleared of deployed resources
```

## Project Structure

```text
config/                       Declarative configuration (all plan-phase inputs)
    templates/
        jinja/                Jinja2 templates for Kubernetes manifests
        values/defaults.yaml  Base configuration with all anchored defaults
    scenarios/                Deployment overrides (guides/, examples/, cicd/)
    specification/            Specification templates (guides/, examples/, cicd/)

llmdbenchmark/                Python package
    cli.py                    Entry point, workspace setup, command dispatch
    config.py                 Plan-phase workspace configuration singleton

    interface/                CLI subcommand definitions (argparse)
        commands.py           Command enum (plan, standup, teardown)
        plan.py               Plan subcommand arguments
        standup.py            Standup subcommand arguments
        teardown.py           Teardown subcommand arguments

    parser/                   Plan-phase template rendering
        render_specification.py   Specification file parsing and validation
        render_plans.py           Jinja2 template rendering engine
        render_result.py          Structured error tracking for renders
        version_resolver.py       Auto-resolve image tags and chart versions

    executor/                 Execution framework (shared across all phases)
        step.py               Step ABC, Phase enum, result dataclasses
        step_executor.py      Step orchestrator (sequential + parallel)
        command.py            kubectl/helm/helmfile subprocess wrapper
        context.py            Shared state (ExecutionContext dataclass)
        deps.py               System dependency checker

    standup/                  Standup phase
        preprocess/           Scripts to be mounted as ConfigMaps in vLLM pods
        steps/                Step implementations (00-10)
            step_00  Validate dependencies, cluster connectivity, kubeconfig
            step_01  Ensure local conda environment for analysis
            step_02  Admin prerequisites (CRDs, gateway, LWS, namespaces)
            step_03  Workload monitoring, node resource discovery
            step_04  Model namespace (PVCs, secrets, download job)
            step_05  Harness namespace (PVC, data access pod, preprocess)
            step_06  Standalone vLLM deployment (Deployment + Service)
            step_07  Helm repos and gateway infrastructure (helmfile)
            step_08  GAIE inference extension deployment
            step_09  Modelservice deployment (helmfile + LWS)
            step_10  Smoketest (endpoint health, model serving validation)

    teardown/                 Teardown phase
        steps/                Step implementations (00-04)
            step_00  Validate cluster connectivity, load teardown config
            step_01  Uninstall Helm releases and routes
            step_02  Clean harness resources (ConfigMaps, pods, secrets)
            step_03  Delete namespaced resources (normal or deep mode)
            step_04  Clean cluster-scoped roles and bindings (admin only)

    logging/                  llmdbenchmark logger (...TODO: need to very threading on parallel standup...)
    exceptions/               Error hierarchy (Template, Configuration, Execution)
    utilities/
        kubernetes.py         Kubernetes Python client helpers (connect, detect OpenShift)
        os/
            filesystem.py     Workspace and directory management
            platform.py       Host OS detection and user identification
```

### Adding a New Step

1. Create a step file in the appropriate phase directory:
   - Standup: `llmdbenchmark/standup/steps/step_NN_your_step.py`
   - Teardown: `llmdbenchmark/teardown/steps/step_NN_your_step.py`
2. Subclass `Step`, set `number`, `name`, `phase`, and `per_stack`
3. Implement `execute(context, stack_path)` returning a `StepResult`
4. Optionally override `should_skip(context)` for conditional execution
5. Register the step in the phase's `steps/__init__.py`

The `Step` base class provides shared helpers: `_load_plan_config()`, `_load_stack_config()`, `_find_rendered_yaml()`, and `_find_yaml()`.

### Deployment Methods

The standup phase supports two deployment paths:

- **standalone** -- Direct Kubernetes Deployments and Services for each model (steps 06)
- **modelservice** -- Helm-based deployment with gateway infrastructure, GAIE, and LWS support (steps 07-09)

Both paths share steps 00-05 (infrastructure, namespaces, secrets) and step 10 (smoketest).

### Teardown

The teardown phase reverses a standup. It operates in two modes:

- **Normal mode** (default) -- Removes only resources matching the deployment method (standalone or modelservice patterns). Preserves system ConfigMaps and the HuggingFace token secret.
- **Deep mode** (`--deep`) -- Deletes all resources of every kind in both namespaces, leaving them empty.

Teardown steps:

| Step | Description | Condition |
|------|-------------|-----------|
| 00 | Validate cluster connectivity, load config | Always |
| 01 | Uninstall Helm releases, delete routes and jobs | Modelservice only |
| 02 | Clean harness ConfigMaps, pods, secrets | Always |
| 03 | Delete namespaced resources (normal or deep) | Always |
| 04 | Clean cluster-scoped ClusterRoles/Bindings | Admin + modelservice only |

## Main Concepts

### [Scenarios](docs/standup.md#scenarios)

Cluster-specific configuration: GPU model, LLM, and `llm-d` parameters.

### [Harnesses](docs/run.md#harnesses)

Load generators that drive benchmark traffic. Supported: [inference-perf](https://github.com/kubernetes-sigs/inference-perf), [guidellm](https://github.com/vllm-project/guidellm.git), [vllm benchmarks](https://github.com/vllm-project/vllm.git), [inferencemax](https://github.com/InferenceMAX/InferenceMAX.git), and nop (for model load time benchmarking).

### (Workload) [Profiles](docs/run.md#profiles)

Benchmark load specifications including LLM use case, traffic pattern, input/output distribution, and dataset. Found under [`workload/profiles`](./workload/profiles).

### [Experiments](docs/doe.md)

Design of Experiments (DOE) files describing parameter sweeps across standup and run configurations.

## Dependencies

- [llm-d-infra](https://github.com/llm-d-incubation/llm-d-infra.git)
- [llm-d-modelservice](https://github.com/llm-d/llm-d-model-service.git)
- [inference-perf](https://github.com/kubernetes-sigs/inference-perf)

## Topics

- [Reproducibility](docs/reproducibility.md)
- [Observability](docs/observability.md)
- [Quickstart](docs/quickstart.md)
- [Resource Requirements](docs/resource_requirements.md)
- [FAQ](docs/faq.md)

## Contribute

- [How to contribute](CONTRIBUTING.md), including development process and governance.
- Join [Slack](https://llm-d.ai/slack) (`sig-benchmarking` channel) for cross-org development discussion.
- Bi-weekly contributor standup: Tuesdays 13:00 EST. [Calendar](https://calendar.google.com/calendar/u/0?cid=NzA4ZWNlZDY0NDBjYjBkYzA3NjdlZTNhZTk2NWQ2ZTc1Y2U5NTZlMzA5MzhmYTAyZmQ3ZmU1MDJjMDBhNTRiNEBncm91cC5jYWxlbmRhci5nb29nbGUuY29t) | [Meeting notes](https://docs.google.com/document/d/1njjeyBJF6o69FlyadVbuXHxQRBGDLcIuT7JHJU3T_og/edit?usp=sharing) | [Google group](https://groups.google.com/g/llm-d-contributors)

## License

Licensed under Apache License 2.0. See [LICENSE](LICENSE) for details.
