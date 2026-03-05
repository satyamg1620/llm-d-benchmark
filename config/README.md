# Configuration

All declarative configuration for `llmdbenchmark` lives in this directory. The three subdirectories correspond to the inputs consumed by the plan phase rendering pipeline.

## Directory Layout

```text
config/
    templates/
        jinja/                  Jinja2 templates that produce Kubernetes manifests
            _macros.j2          Shared macros (vLLM command generation, etc.)
            01_pvc_workload-pvc.yaml.j2    ... through 16_pvc_extra-pvc.yaml.j2
        values/
            defaults.yaml       Base configuration with all anchored defaults

    scenarios/                  Deployment overrides (merged on top of defaults)
        guides/                 Well-lit-path guide scenarios
        examples/               Minimal working examples (cpu, gpu, spyre)
        cicd/                   CI/CD pipeline environments

    specification/              Plan specifications (entry points for the CLI)
        guides/                 Well-lit-path guide specifications
        examples/               Minimal working specifications
        cicd/                   CI/CD pipeline specifications
```

## How the Pieces Fit Together

```text
specification.yaml.j2   (entry point — tells the tool where to find everything)
        |
        v
defaults.yaml           (base values — config/templates/values/defaults.yaml)
     +
scenario.yaml           (overrides — config/scenarios/**/*.yaml)
     +
templates/jinja/*.j2    (Jinja2 templates — produce K8s manifests)
        |
        v
  [plan phase]           Merges defaults + scenario, renders templates
        |
        v
  rendered stacks/       Static Kubernetes YAMLs ready to apply
```

---

## Templates

### `templates/jinja/`

Jinja2 templates that produce Kubernetes resource definitions. Each template corresponds to a specific infrastructure component:

| Template | Output |
|----------|--------|
| `01_pvc_workload-pvc.yaml.j2` | Workload PVC for harness data |
| `02_pvc_model-pvc.yaml.j2` | Model storage PVC |
| `03_cluster-monitoring-config.yaml.j2` | OpenShift workload monitoring config |
| `04_download_job.yaml.j2` | Model download Job |
| `05_namespace_sa_rbac_secret.yaml.j2` | Namespace, ServiceAccount, RBAC, secrets |
| `06_pod_access_to_harness_data.yaml.j2` | Harness data access pod |
| `07_service_access_to_harness_data.yaml.j2` | Harness data access service |
| `08_httproute.yaml.j2` | HTTPRoute for inference gateway |
| `09_helmfile-gateway-provider.yaml.j2` | Helmfile for gateway provider (Istio/kgateway) |
| `10_helmfile-main.yaml.j2` | Main helmfile (llm-d-infra, modelservice) |
| `11_infra.yaml.j2` | Infrastructure chart values |
| `12_gaie-values.yaml.j2` | GAIE (inference extension) Helm values |
| `13_ms-values.yaml.j2` | Modelservice Helm values |
| `14_standalone-deployment_yaml.j2` | Standalone vLLM Deployment |
| `15_standalone-service_yaml.j2` | Standalone vLLM Service |
| `16_pvc_extra-pvc.yaml.j2` | Extra PVCs (e.g., scratch space) |
| `_macros.j2` | Shared Jinja2 macros |

### `templates/values/defaults.yaml`

The base configuration file containing every configurable parameter with sensible defaults. Uses YAML anchors extensively for DRY references across sections. Scenario files override only the values they need to change.

---

## Scenarios

Scenario files provide deployment-specific overrides that are merged on top of `defaults.yaml`. They configure things like model name, GPU count, namespace, image tags, and deployment topology.

### `scenarios/guides/`

Map directly to the [llm-d well-lit-path guides](https://github.com/llm-d/llm-d/tree/main/guides). Each scenario reproduces the deployment described in its corresponding guide.

| Scenario | Description |
|----------|-------------|
| `inference-scheduling.yaml` | Qwen3-32B with inference scheduling plugins |
| `pd-disaggregation.yaml` | Prefill/decode disaggregation |
| `precise-prefix-cache-aware.yaml` | Prefix cache aware routing |
| `tiered-prefix-cache.yaml` | Tiered CPU/GPU prefix cache |
| `wide-ep-lws.yaml` | Expert parallelism with LeaderWorkerSet |
| `simulated-accelerators.yaml` | CPU-only simulation with opt-125m |

### `scenarios/examples/`

Minimal starting points for common hardware:

| Scenario | Description |
|----------|-------------|
| `cpu.yaml` | CPU-only deployment (no GPU) |
| `gpu.yaml` | Standard GPU deployment |
| `spyre.yaml` | IBM Spyre accelerator |

### `scenarios/cicd/`

Used by automated CI/CD pipelines:

| Scenario | Description |
|----------|-------------|
| `kind-sim.yaml` | Kind cluster with simulated accelerators |
| `gke-h100.yaml` | Google Kubernetes Engine with H100 |
| `cks.yaml` | Cloud Kubernetes Service with H200 |
| `ocp.yaml` | OpenShift Container Platform with Istio |

---

## Specifications

Specification files are the entry points for the CLI. Each is a Jinja2 template (`.yaml.j2`) that declares paths to the defaults, templates, and scenario files, plus optional experiment definitions.

### Required Fields

Every specification must declare three paths:

```yaml
{% set base_dir = base_dir | default('../') -%}
base_dir: {{ base_dir }}

values_file:
  path: {{ base_dir }}/config/templates/values/defaults.yaml

template_dir:
  path: {{ base_dir }}/config/templates/jinja
```

### Optional Fields

```yaml
scenario_file:
  path: {{ base_dir }}/config/scenarios/guides/inference-scheduling.yaml

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

### The `base_dir` Variable

All paths are relative to `base_dir`, which defaults to `../` (the repository root when running from the repo directory). Override it with `--bd`:

```bash
llmdbenchmark --bd /path/to/repo --spec config/specification/guides/inference-scheduling.yaml.j2 plan
```

### Creating a New Specification

1. Create a scenario YAML under `config/scenarios/` with your deployment overrides
2. Create a specification template under `config/specification/`:

```yaml
{% set base_dir = base_dir | default('../') -%}
base_dir: {{ base_dir }}

values_file:
  path: {{ base_dir }}/config/templates/values/defaults.yaml

template_dir:
  path: {{ base_dir }}/config/templates/jinja

scenario_file:
  path: {{ base_dir }}/config/scenarios/my-scenario.yaml
```

3. Run: `llmdbenchmark --spec config/specification/my-spec.yaml.j2 plan`

### Experiments

To add parameter sweeps, include an `experiments` section. Experiments have two attribute categories:

- **`setup`** -- Parameters that change the deployment (e.g., replicas, scheduler plugin). Each treatment generates a separate rendered stack.
- **`run`** -- Parameters that change the benchmark workload (e.g., concurrency, prompt length). Used during the run phase, not standup.

Each category contains:

| Field | Purpose |
|-------|---------|
| `factors` | Parameters being varied, each with a list of `levels` (possible values) |
| `constants` | Fixed parameters applied to every treatment (optional) |
| `treatments` | Explicit combinations of factor levels to test |

### Available Specifications

**Guides:**

| Specification | Experiments |
|---------------|-------------|
| `inference-scheduling.yaml.j2` | GAIE plugin configs x prompt/output lengths |
| `pd-disaggregation.yaml.j2` | Deployment method, replicas, TP sizes x concurrency |
| `precise-prefix-cache-aware.yaml.j2` | GAIE prefix cache configs x prompt groups |
| `tiered-prefix-cache.yaml.j2` | CPU block sizes x prompt groups |
| `wide-ep-lws.yaml.j2` | Standup only |
| `simulated-accelerators.yaml.j2` | Standup only |

**Examples:** `cpu.yaml.j2`, `gpu.yaml.j2`, `spyre.yaml.j2`

**CI/CD:** `cks.yaml.j2`, `gke-h100.yaml.j2`, `kind-sim.yaml.j2`, `ocp.yaml.j2`

---

## Usage

```bash
# Plan (render templates into manifests)
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 plan

# Standup (plan + apply to cluster)
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 standup

# Dry run
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 --dry-run standup

# Teardown
llmdbenchmark --spec config/specification/guides/inference-scheduling.yaml.j2 teardown
```
