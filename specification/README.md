# Specification Templates

Specification files tell `llmdbenchmark` where to find its inputs: default values, Jinja2 templates, scenario overrides, and optional experiment definitions. Each specification is a Jinja2 template (`.yaml.j2`) that gets rendered into a plain YAML document during the plan phase.

## Directory Layout

```text
specification/
    guides/                 Specifications for llm-d well-lit-path guides
        inference-scheduling.yaml.j2
        pd-disaggregation.yaml.j2
        precise-prefix-cache-aware.yaml.j2
        tiered-prefix-cache.yaml.j2
        wide-ep-lws.yaml.j2
        simulated-accelerators.yaml.j2
    examples/               Minimal working specifications for common hardware
        cpu.yaml.j2
        gpu.yaml.j2
        spyre.yaml.j2
    cicd/                   CI/CD pipeline specifications
        cks.yaml.j2
        gke-h100.yaml.j2
        kind-sim.yaml.j2
        ocp.yaml.j2
    README.md               
```

- **guides/** -- Map directly to the [llm-d guides](https://github.com/llm-d/llm-d/tree/main/guides). Each specification pairs a scenario with optional experiment definitions to reproduce the guide's benchmark.
- **examples/** -- Starting points for users deploying on CPU, GPU, or IBM Spyre hardware. No experiments are defined; they only stand up a stack.
- **cicd/** -- Used by automated CI/CD pipelines targeting specific cluster environments.

## How Specifications Work

A specification connects four things:

```text
defaults.yaml        The base configuration (templates/values/defaults.yaml)
     +
scenario.yaml        Overrides for a specific deployment (scenarios/**/*.yaml)
     +
templates/jinja/     Jinja2 templates that produce Kubernetes manifests
     +
experiments          Optional parameter sweeps (embedded in the specification)
     |
     v
  [plan phase]       llmdbenchmark renders all of the above into static YAMLs
     |
     v
  rendered stacks/   Ready-to-apply Kubernetes manifests
```

During the plan phase, scenario values override matching keys from `defaults.yaml`. The merged values are then fed into the Jinja2 templates to produce the final Kubernetes manifests. If experiments are defined, the plan generates one rendered stack per treatment combination.

## Specification Structure

### Required Fields

Every specification must declare three paths:

```yaml
# Base directory -- all other paths are relative to this
{% set base_dir = base_dir | default('../') -%}
base_dir: {{ base_dir }}

# Path to the defaults file
values_file:
  path: {{ base_dir }}/templates/values/defaults.yaml

# Directory containing Jinja2 templates
template_dir:
  path: {{ base_dir }}/templates/jinja
```

### Optional Fields

```yaml
# Scenario file with values that override defaults
scenario_file:
  path: {{ base_dir }}/scenarios/guides/inference-scheduling.yaml

# Experiment definitions for parameter sweeps
experiments:
  - name: "experiment-name"
    attributes:
      - name: "setup"
        factors: [...]
        treatments: [...]
      - name: "run"
        factors: [...]
        treatments: [...]
```

If `scenario_file` is omitted, only the default values are used. If `experiments` is omitted, a single stack is rendered (no parameter sweeps).

## The `base_dir` Variable

All paths in a specification are relative to `base_dir`. The Jinja2 line at the top of every specification handles this:

```yaml
{% set base_dir = base_dir | default('../') -%}
```

The default `../` is correct when running from the repository root (since specifications live in `specification/`). To override it, use the CLI flag:

```bash
llmdbenchmark --bd /path/to/repo --spec specification/guides/inference-scheduling.yaml.j2 plan
```

This allows users with custom directory layouts to point to their own defaults, templates, and scenarios without modifying the specification file.

## Creating a New Specification

### Standup Only (No Experiments)

For a specification that stands up a single stack without experiments:

1. Create a scenario YAML under `scenarios/` with your deployment overrides (model, GPU count, namespace, etc.)
2. Create a specification template:

```yaml
# My Custom Deployment
# Deploys <model> on <hardware>.

# [REQUIRED]
{% set base_dir = base_dir | default('../') -%}
base_dir: {{ base_dir }}

# [REQUIRED]
values_file:
  path: {{ base_dir }}/templates/values/defaults.yaml

# [REQUIRED]
template_dir:
  path: {{ base_dir }}/templates/jinja

# [OPTIONAL]
scenario_file:
  path: {{ base_dir }}/scenarios/my-scenario.yaml
```

3. Run the plan:

```bash
llmdbenchmark --spec specification/my-spec.yaml.j2 plan
```

### With Experiments

To add parameter sweeps, include an `experiments` section. Experiments have two attribute categories:

- **`setup`** -- Parameters that change the deployment (e.g., number of replicas, scheduler plugin). Each treatment generates a separate rendered stack.
- **`run`** -- Parameters that change the benchmark workload (e.g., concurrency, prompt length). These are used during the run phase, not during standup.

Each attribute category contains:

| Field | Purpose |
|-------|---------|
| `factors` | Parameters being varied, each with a list of `levels` (possible values) |
| `constants` | Fixed parameters applied to every treatment (optional) |
| `treatments` | Explicit combinations of factor levels to test |

Example with both setup and run experiments:

```yaml
experiments:
  - name: "my-experiment"
    attributes:
      # Setup factors change the deployment
      - name: "setup"
        factors:
          - name: inferenceExtension.pluginsConfigFile
            levels:
              - none.yaml
              - prefix.yaml
              - kv.yaml
        treatments:
          - "none.yaml"
          - "prefix.yaml"
          - "kv.yaml"

      # Run factors change the benchmark workload
      - name: "run"
        constants:
          - streaming: true
        factors:
          - name: question_len
            levels: [100, 300, 1000]
          - name: output_len
            levels: [100, 300]
        treatments:
          - "100,100"
          - "100,300"
          - "300,100"
          - "300,300"
          - "1000,100"
          - "1000,300"
```

## Usage

Plan a deployment (renders templates into Kubernetes manifests):

```bash
llmdbenchmark --spec specification/guides/inference-scheduling.yaml.j2 plan
```

Stand up the deployment (plans + applies to cluster):

```bash
llmdbenchmark --spec specification/guides/inference-scheduling.yaml.j2 standup
```

Dry run (renders all manifests without touching the cluster):

```bash
llmdbenchmark --spec specification/guides/inference-scheduling.yaml.j2 --dry-run standup
```

Override `base_dir` for custom layouts:

```bash
llmdbenchmark --bd /my/custom/repo --spec specification/guides/inference-scheduling.yaml.j2 plan
```

## Available Specifications

### Guides

| Specification | Scenario | Experiments |
|---------------|----------|-------------|
| `inference-scheduling.yaml.j2` | Qwen3-32B with inference scheduling plugins | GAIE plugin configs x prompt/output lengths |
| `pd-disaggregation.yaml.j2` | Prefill/decode disaggregation | Deployment method, replicas, TP sizes x concurrency |
| `precise-prefix-cache-aware.yaml.j2` | Prefix cache aware routing | GAIE prefix cache configs x prompt groups |
| `tiered-prefix-cache.yaml.j2` | Tiered CPU/GPU prefix cache | CPU block sizes x prompt groups |
| `wide-ep-lws.yaml.j2` | Expert parallelism with LeaderWorkerSet | Standup only |
| `simulated-accelerators.yaml.j2` | CPU-only simulation with opt-125m | Standup only |

### Examples

| Specification | Description |
|---------------|-------------|
| `cpu.yaml.j2` | CPU-only deployment (no GPU) |
| `gpu.yaml.j2` | Standard GPU deployment |
| `spyre.yaml.j2` | IBM Spyre accelerator deployment |

### CI/CD

| Specification | Description |
|---------------|-------------|
| `cks.yaml.j2` | Cloud Kubernetes Service with H200 |
| `gke-h100.yaml.j2` | Google Kubernetes Engine with H100 |
| `kind-sim.yaml.j2` | Kind cluster with simulated accelerators |
| `ocp.yaml.j2` | OpenShift Container Platform with Istio |
