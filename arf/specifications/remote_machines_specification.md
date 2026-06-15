# Remote Machines Specification

**Version**: 5

* * *

## Purpose

This specification defines the protocol, data formats, and verification rules for provisioning and
managing remote GPU machines during task execution. It covers the full machine lifecycle from search
through destruction, the provider routing rule, the price/performance decision protocol, and how
machine data integrates with the existing cost and results tracking.

**Producer**: The `setup-machines` and `teardown` steps of the task execution skill, implemented by
the `/setup-remote-machine` skill. Three Python helpers back the protocol:
`arf/scripts/utils/vast_machines.py` (Vast.ai provider), `arf/scripts/utils/azure_ml_vm.py` (Azure
ML pool), and `arf/scripts/utils/nebius_machines.py` (Nebius Cloud).

**Consumers**:

* **Task execution agents** — follow this protocol during GPU tasks
* **Verificator scripts** — confirm machines are destroyed and costs logged
* **Aggregator scripts** — collect machine usage data across tasks (`aggregate_machines`,
  `aggregate_costs`)
* **Human reviewers** — audit GPU spend at checkpoints

* * *

## Provider Routing

The project supports three GPU providers: **Vast.ai** (slug `vast_ai`), **Azure ML** (slug
`azure_ml`), and **Nebius Cloud** (slug `nebius`). Provider selection is driven by the `gpu_class`
field declared in `plan.md` Section 5 (Remote Machines), with a `provider:` field that is required
whenever more than one provider can serve the requested `gpu_class`.

### Routing rule

| `gpu_class` declared in plan.md | Provider chosen | Notes |
| --- | --- | --- |
| `H100` | `azure_ml` (default) or explicit `nebius` | 2×H100 Azure ML pool from `project/azure_vm.json`; Nebius EU has H100 too — choose explicitly when both are acceptable |
| `H200`, `B200`, `B300`, `MI300`, `GB200`, or any GPU more powerful than H100 | `vast_ai` or `nebius` | **Must declare `provider:` explicitly** — no auto-routing between Vast and Nebius |
| `A100`, `L40S`, `RTX_4090`, or any GPU less powerful than H100 | author's choice | Must be declared explicitly via `provider:` field in plan.md |

`gpu_class` is a free-form upper-case label that the author writes in plan.md. The routing rule is
enforced by `/setup-remote-machine` Phase 1. Whenever multiple providers can serve a class, an
explicit `provider:` field is mandatory; omitting it is a hard error. A plan that names a
provider/GPU combination that does not exist in that provider's catalogue (e.g. `provider: azure_ml`
with `gpu_class: H200`) is also a hard error.

### Why route by GPU class

* The Azure ML pool is a fixed-size set of NC80-class (2×H100 80GB) VMs. It cannot serve other GPU
  classes.
* Vast.ai is a spot marketplace with current and future GPU classes the Azure pool does not stock
  (H200, B200, MI300, GB200, and so on).
* Nebius Cloud is a managed-cloud alternative with on-demand H100, H200, B200, B300, L40S in
  multiple regions (eu-north1 / eu-west1 / us-central1 / me-west1 / uk-south1). Use it when EU
  residency matters, when a stable IP / managed-cloud SLA is required, or when Vast supply is thin.
* For sub-H100 work and for GPUs available on multiple providers, the trade-off is real (per-hour
  price, reliability, region) so the plan author makes the call explicitly.

* * *

## Machine Lifecycle

Every remote machine goes through these states in order:

```text
searching -> creating -> waiting -> ready -> in-use -> destroying -> destroyed
```

| State | Description |
| --- | --- |
| `searching` | Querying provider for available offers (Vast) or pool entries (Azure) |
| `creating` | Instance creation request sent (Vast) or pool VM start request issued (Azure) |
| `waiting` | Instance created, waiting for `actual_status == "running"` and SSH |
| `ready` | SSH verified, GPU confirmed, environment prepared |
| `in-use` | Task execution running on the machine |
| `destroying` | Destruction (Vast) or deallocation (Azure) request sent |
| `destroyed` | Instance confirmed destroyed (Vast) or stopped (Azure) |

A machine must reach `destroyed` before the task can be marked complete. Any machine that does not
reach `destroyed` is a verification error.

* * *

## machine_log.json

The primary tracking file for remote machine usage. Created during the `setup-machines` step and
updated during `teardown`. Located in the setup-machines step log directory:

```text
tasks/<task_id>/logs/steps/NNN_setup-machines/machine_log.json
```

### Schema

A JSON array of machine objects. Most tasks use a single machine, but multi-GPU distributed tasks
may use several.

### Fields (per machine object)

| Field | Type | Required | Applies to | Set during | Description |
| --- | --- | --- | --- | --- | --- |
| `spec_version` | string | yes (v5+) | all | creating | Spec version (e.g., `"5"`). Legacy entries with `"4"` or earlier or no field are treated permissively (warning only). |
| `provider` | string enum | yes | all | creating | One of `"vast_ai"`, `"azure_ml"`, or `"nebius"` |
| `instance_id` | string | yes | all | creating | Provider-specific instance ID (Vast offer instance ID, Azure VM name, or Nebius `computeinstance-...` ID) |
| `selected_offer` | object | yes | both | searching | Details of the chosen offer / pool VM (see below) |
| `selection_rationale` | string | yes | both | searching | Why this offer / pool VM was chosen |
| `image` | string | Vast only | Vast | creating | Docker image used (Azure pool image is fixed, recorded in `selected_offer.image`) |
| `disk_gb` | int | yes | both | creating | Disk space allocated in GB |
| `label` | string\|null | no | both | creating | Instance label in provider dashboard (e.g., `"my-project/t0042_train"`) |
| `ssh_host` | string | yes | both | ready | SSH hostname (Vast assigned host or Azure `ssh_host_alias` from `~/.ssh/config`) |
| `ssh_port` | int | yes | both | ready | SSH port |
| `gpu_verified` | string | yes | both | ready | GPU model confirmed by `nvidia-smi` |
| `cuda_version` | string | yes | both | ready | CUDA version from `nvidia-smi` |
| `created_at` | string | yes | both | creating | ISO 8601 timestamp |
| `ready_at` | string | yes | both | ready | ISO 8601 timestamp |
| `destroyed_at` | string\|null | yes | both | destroyed | ISO 8601 timestamp, `null` until destroyed |
| `total_duration_hours` | float\|null | yes | both | destroyed | Hours from `created_at` to `destroyed_at` |
| `total_cost_usd` | float\|null | yes | both | destroyed | Final cost from provider, `null` until known |
| `search_started_at` | string | yes | both | searching | ISO 8601 timestamp when offer search / pool walk began |
| `total_provisioning_seconds` | float | yes | both | ready | Wall-clock seconds from `search_started_at` to `ready_at` |
| `failed_attempts` | array | yes | both | searching–ready | Failed provisioning attempts before success (see below) |
| `offer_id` | int | Vast only | Vast | creating | Vast.ai offer ID used to create the instance |
| `search_criteria` | object | Vast only | Vast | searching | Vast.ai search filters used (see below) |
| `vm_name` | string\|null | no | Azure | creating | Azure VM resource name (e.g., `arf-NC80-weu-v1`) — mirrors `instance_id` for Azure pool entries |
| `hourly_cost_usd` | float | Azure only | Azure | creating | Fixed pool rate read from `project/azure_vm.json` |
| `started_vm` | bool | Azure only | Azure | creating | `true` when this acquire issued `az ml compute start`, `false` when the VM was already running |
| `platform` | string | Nebius only | Nebius | creating | Nebius compute platform slug (e.g., `gpu-h200-sxm`, `gpu-h100-sxm`) |
| `preset` | string | Nebius only | Nebius | creating | Nebius compute preset slug (e.g., `1gpu-16vcpu-200gb`, `8gpu-128vcpu-1600gb`) |
| `image_id` | string | Nebius only | Nebius | creating | Nebius image ID (e.g., `computeimage-e00ckjm576pedy5rwv` for `ubuntu22.04-cuda12`) |
| `region` | string | Nebius only | Nebius | creating | Nebius region (e.g., `eu-north1`, `eu-west1`) |
| `tenant_id` | string | Nebius only | Nebius | creating | Nebius tenant ID (`tenant-...`) the instance was created under |
| `project_id` | string | Nebius only | Nebius | creating | Nebius project ID (`project-...`) the instance was created under |
| `public_ip` | string\|null | Nebius only | Nebius | creating | Public IPv4 assigned by Nebius; `null` when the instance is private-only |
| `boot_disk_id` | string | Nebius only | Nebius | creating | Boot disk ID (`computedisk-...`) — auto-deleted with the instance, recorded for forensic audit |
| `checkpoint_path` | string\|null | no | all | ready | Remote path where training checkpoints are saved |
| `heartbeat_path` | string\|null | no | all | ready | Remote path to heartbeat file updated by training script |
| `smoke_test_output` | string\|null | no | all | ready | Verbatim stdout/stderr of the end-to-end CUDA smoke test run inside the target venv during the `setup-remote-machine` smoke gate (see `arf/skills/setup-remote-machine/SKILL.md`). When set, must include the output of a minimal device check such as `python -c "import torch; assert torch.cuda.is_available(); assert torch.cuda.device_count() == <N>"` plus any serving-engine version line the task installs. `null` when no smoke test was run. See `LESSONS.md` Lesson 2 (smoke-gate before measurement). |

New v5 entries set `spec_version: "5"`. The v5 bump adds the `nebius` provider enum value and its
Nebius-only fields (`platform`, `preset`, `image_id`, `region`, `tenant_id`, `project_id`,
`public_ip`, `boot_disk_id`). Pre-v5 entries remain readable: verificators emit a warning
(`RM-W007`) and treat absent fields permissively. The `v2`-marked fields from older versions
(`search_started_at`, `total_provisioning_seconds`, `failed_attempts`) remain required across all
providers.

### selected_offer Object — Vast.ai

| Field | Type | Description |
| --- | --- | --- |
| `offer_id` | int | Vast.ai offer ID |
| `gpu` | string | GPU model name |
| `gpu_count` | int | Number of GPUs |
| `gpu_ram_gb` | float | Per-GPU VRAM in GB |
| `cpu_ram_gb` | float | Total system RAM in GB |
| `disk_gb` | float | Available disk space in GB |
| `price_per_hour` | float | Total $/hour (GPU + storage) |
| `reliability` | float | Machine reliability score |
| `location` | string | Geographic location |

### selected_offer Object — Azure ML

| Field | Type | Description |
| --- | --- | --- |
| `vm_name` | string | Pool VM name (e.g., `arf-NC80-weu-v1`) |
| `priority` | int | Pool priority position (1 = primary) |
| `gpu` | string | GPU model name (always `"H100"` for the current pool) |
| `gpu_count` | int | Number of GPUs (always `2` for the NC80 pool) |
| `gpu_ram_gb` | float | Per-GPU VRAM in GB (always `80.0`) |
| `cpu_ram_gb` | float | Total system RAM in GB |
| `disk_gb` | float | OS disk size in GB |
| `image` | string | Azure ML image reference (e.g., `26.01.05`) |
| `region` | string | Azure region (e.g., `westeurope`) |
| `hourly_cost_usd` | float | Fixed rate from `project/azure_vm.json` |

### selected_offer Object — Nebius

| Field | Type | Description |
| --- | --- | --- |
| `platform` | string | Compute platform slug (e.g., `gpu-h200-sxm`, `gpu-h100-sxm`) |
| `preset` | string | Preset slug (e.g., `1gpu-16vcpu-200gb`, `8gpu-128vcpu-1600gb`) |
| `gpu` | string | GPU model name derived from `platform` (e.g., `"H200"`, `"H100"`) |
| `gpu_count` | int | Number of GPUs in the preset |
| `gpu_ram_gb` | float | Per-GPU VRAM in GB (e.g., `141.0` for H200, `80.0` for H100) |
| `cpu_ram_gb` | float | Preset's RAM size in GB |
| `disk_gb` | float | Boot disk size in GB chosen at acquire time |
| `image_id` | string | Nebius image ID (`computeimage-...`) used for the boot disk |
| `region` | string | Nebius region (e.g., `eu-north1`) |
| `hourly_cost_estimate_usd` | float\|null | Best-effort price estimate at acquire time; `null` when Nebius does not publish pricing via the CLI (Nebius does not currently expose pricing in the CLI, so this is usually `null` and the cost is reconciled at teardown from billing data) |

### search_criteria Object (Vast only)

| Field | Type | Description |
| --- | --- | --- |
| `gpu_name` | string\|null | Required GPU model (e.g., `"H200"`) |
| `num_gpus` | int | Number of GPUs needed |
| `min_gpu_ram` | float\|null | Minimum per-GPU VRAM in GB |
| `min_cpu_ram` | float\|null | Minimum system RAM in GB |
| `min_disk` | float\|null | Minimum disk space in GB |
| `min_reliability` | float | Minimum reliability score (see thresholds below) |
| `extra_filters` | string\|null | Any additional vastai query filters |

### failed_attempts Array

Each element records one provisioning attempt that failed before the successful machine was created.
The array is empty (`[]`) when the first attempt succeeds.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `provider_ref` | string | yes | Vast offer ID as string, or Azure VM name |
| `instance_id` | string\|null | yes | Instance ID if creation succeeded before failure; `null` if creation itself failed |
| `gpu` | string | yes | GPU model of the attempted offer / pool VM |
| `failure_reason` | string | yes | Human-readable description of what went wrong |
| `failure_phase` | string | yes | One of: `"search"`, `"creation"`, `"waiting"`, `"gpu_verification"`, `"ssh"`, `"lock_held"` |
| `duration_seconds` | float | yes | Seconds spent on this attempt before abandoning |
| `wasted_cost_usd` | float | yes | Cost incurred by this failed attempt (`0.0` if instance never reached billing) |
| `timestamp` | string | yes | ISO 8601 timestamp when the attempt started |

### Example — Vast.ai (2×H200)

```json
[
  {
    "spec_version": "5",
    "provider": "vast_ai",
    "instance_id": "34201117",
    "offer_id": 34199002,
    "search_criteria": {
      "gpu_name": "H200",
      "num_gpus": 2,
      "min_gpu_ram": 141.0,
      "min_cpu_ram": 256.0,
      "min_disk": 500.0,
      "min_reliability": 0.995,
      "extra_filters": null
    },
    "selected_offer": {
      "offer_id": 34199002,
      "gpu": "H200",
      "gpu_count": 2,
      "gpu_ram_gb": 141.0,
      "cpu_ram_gb": 384.0,
      "disk_gb": 1000.0,
      "price_per_hour": 4.78,
      "reliability": 0.998,
      "location": "Frankfurt, DE"
    },
    "selection_rationale": "Estimated 3h benchmark. 2xH200 at $4.78/h = $14.34 total. Next option (2xH100 SXM at $4.20/h) is ~25% slower, netting ~$3 more total.",
    "image": "vllm/vllm-openai:v0.21.0",
    "disk_gb": 500,
    "label": "<project>/t0040_h200_baseline",
    "ssh_host": "ssh4.vast.ai",
    "ssh_port": 12117,
    "gpu_verified": "NVIDIA H200",
    "cuda_version": "13.0",
    "created_at": "2026-05-21T09:10:00Z",
    "ready_at": "2026-05-21T09:18:30Z",
    "destroyed_at": "2026-05-21T12:25:00Z",
    "total_duration_hours": 3.24,
    "total_cost_usd": 15.49,
    "search_started_at": "2026-05-21T09:05:00Z",
    "total_provisioning_seconds": 810.0,
    "failed_attempts": [],
    "checkpoint_path": null,
    "heartbeat_path": "/home/vastai/heartbeat.json",
    "smoke_test_output": "torch 2.11.0+cu130 cuda True devs 2\nvllm 0.21.0\n"
  }
]
```

### Example — Azure ML (2×H100 from pool)

```json
[
  {
    "spec_version": "5",
    "provider": "azure_ml",
    "instance_id": "arf-NC80-weu-v1",
    "vm_name": "arf-NC80-weu-v1",
    "hourly_cost_usd": 13.96,
    "started_vm": true,
    "selected_offer": {
      "vm_name": "arf-NC80-weu-v1",
      "priority": 1,
      "gpu": "H100",
      "gpu_count": 2,
      "gpu_ram_gb": 80.0,
      "cpu_ram_gb": 880.0,
      "disk_gb": 1024.0,
      "image": "26.01.05",
      "region": "westeurope",
      "hourly_cost_usd": 13.96
    },
    "selection_rationale": "Pool priority 1 (westeurope), no lock present, started from stopped state.",
    "disk_gb": 1024,
    "label": "<project>/t0042_h100_benchmark",
    "ssh_host": "arf-NC80-weu-v1",
    "ssh_port": 22,
    "gpu_verified": "NVIDIA H100 80GB HBM3",
    "cuda_version": "12.2",
    "created_at": "2026-05-21T09:00:00Z",
    "ready_at": "2026-05-21T09:06:40Z",
    "destroyed_at": "2026-05-21T11:30:00Z",
    "total_duration_hours": 2.50,
    "total_cost_usd": 34.90,
    "search_started_at": "2026-05-21T08:58:00Z",
    "total_provisioning_seconds": 520.0,
    "failed_attempts": [],
    "checkpoint_path": null,
    "heartbeat_path": null,
    "smoke_test_output": "torch 2.11.0+cu129 cuda True devs 2\nvllm 0.20.2+cu129\n"
  }
]
```

### Example — Nebius Cloud (1×H200, eu-north1)

```json
[
  {
    "spec_version": "5",
    "provider": "nebius",
    "instance_id": "computeinstance-e00mzby80wfn7y0ahr",
    "platform": "gpu-h200-sxm",
    "preset": "1gpu-16vcpu-200gb",
    "image_id": "computeimage-e00ckjm576pedy5rwv",
    "region": "eu-north1",
    "tenant_id": "tenant-e00kvxjdt55t45645f",
    "project_id": "project-e00q35s8pr00secyf54ffx",
    "public_ip": "195.242.28.166",
    "boot_disk_id": "computedisk-e00heghhhs3m1m04md",
    "selected_offer": {
      "platform": "gpu-h200-sxm",
      "preset": "1gpu-16vcpu-200gb",
      "gpu": "H200",
      "gpu_count": 1,
      "gpu_ram_gb": 141.0,
      "cpu_ram_gb": 200.0,
      "disk_gb": 100.0,
      "image_id": "computeimage-e00ckjm576pedy5rwv",
      "region": "eu-north1",
      "hourly_cost_estimate_usd": null
    },
    "selection_rationale": "Single-H200 smoke test in eu-north1 (closest region, 32-GPU quota available).",
    "disk_gb": 100,
    "label": "<project>/t0050_nebius_smoke",
    "ssh_host": "195.242.28.166",
    "ssh_port": 22,
    "gpu_verified": "NVIDIA H200",
    "cuda_version": "12.4",
    "created_at": "2026-06-03T07:28:00Z",
    "ready_at": "2026-06-03T07:30:15Z",
    "destroyed_at": "2026-06-03T07:33:44Z",
    "total_duration_hours": 0.10,
    "total_cost_usd": 0.35,
    "search_started_at": "2026-06-03T07:27:30Z",
    "total_provisioning_seconds": 165.0,
    "failed_attempts": [],
    "checkpoint_path": null,
    "heartbeat_path": null,
    "smoke_test_output": "torch 2.4.0+cu124 cuda True devs 1\n"
  }
]
```

* * *

## Instance Labeling

After creating an instance, label it in the provider dashboard so humans can identify which project
and task each machine belongs to. Use the format `"<project-name>/<task_id>"`.

For Vast.ai:

```bash
vastai label instance <INSTANCE_ID> "<project>/$TASK_ID"
```

For Azure ML, the label is the pool VM's `tags.current_task` field, set by `azure_ml_vm acquire`.

For Nebius, the label is set at create time via `--name` (the human-readable instance name) and via
`--labels project=<project>,task=<TASK_ID>` on the create command. Both surface in the Nebius
console for dashboard readability.

Record the label in `machine_log.json` as the `label` field. This has no effect on billing or
lifecycle — it is purely for dashboard readability.

* * *

## Vast.ai Default Search Filters

Every Vast search query MUST include these filters to prevent known-incompatible hardware:

```text
compute_cap<1200 cuda_max_good>=12.6
```

* `compute_cap<1200` — blocks Blackwell-architecture GPUs (RTX 5090, RTX PRO 6000 S/WS) whose sm_120
  compute capability is not supported by PyTorch 2.6.0 or earlier. Remove this filter only after
  verifying that the task's PyTorch version supports sm_120.
* `cuda_max_good>=12.6` — blocks machines with CUDA drivers too old to run the container image.
  Adjust the version to match the image's CUDA requirement.

These filters are encoded as `DEFAULT_FILTERS` in `arf.scripts.utils.vast_machines` and are applied
automatically when using the library. The filters are Vast-specific — the Azure pool is fixed and
does not use them.

* * *

## Vast.ai Price/Performance Decision Protocol

This subsection applies only to the Vast.ai provider. The Azure ML pool uses a fixed priority walk
(see "Azure ML Pool Selection" below).

The goal is to find the **best price/performance balance** within budget. The user's waiting time
has real cost — spending $3 extra to save 5 hours is almost always worth it. But spending $50 extra
for a 10% speedup is not. Never optimize purely for minimum cost at the expense of the user's time.

### Formula

```text
estimated_total_cost = price_per_hour * estimated_hours(gpu_tier)
```

### GPU Relative Speed Tiers

Use these approximate relative speeds for common training/inference workloads (normalized to RTX
3090 = 1.0x):

| GPU | VRAM | Relative Speed | Typical $/hr Range |
| --- | --- | --- | --- |
| GTX 1080 Ti | 11 GB | 0.35x | $0.04-0.08 |
| RTX 2080 Ti | 11 GB | 0.55x | $0.05-0.10 |
| RTX 3060 | 12 GB | 0.40x | $0.03-0.07 |
| RTX 3070 | 8 GB | 0.60x | $0.06-0.10 |
| RTX 3080 | 10 GB | 0.80x | $0.08-0.15 |
| RTX 3090 | 24 GB | 1.0x | $0.10-0.20 |
| RTX 4070 | 12 GB | 0.90x | $0.06-0.12 |
| RTX 4070 Ti | 12 GB | 1.05x | $0.07-0.15 |
| RTX 4080 | 16 GB | 1.30x | $0.15-0.25 |
| RTX 4090 | 24 GB | 1.60x | $0.20-0.40 |
| RTX 5060 Ti | 16 GB | 1.00x | $0.07-0.20 |
| RTX 5070 Ti | 16 GB | 1.50x | $0.09-0.12 |
| RTX 5090 | 32 GB | 2.50x | $0.30-0.65 |
| A100 40GB | 40 GB | 1.80x | $0.50-1.00 |
| A100 80GB | 80 GB | 2.00x | $0.80-1.50 |
| H100 | 80 GB | 3.00x | $1.50-3.00 |
| H200 | 141GB | 3.50x | $2.30-2.60 |
| RTX PRO 6000 S | 96 GB | 2.80x | $0.73-1.12 |
| RTX PRO 6000 WS | 96 GB | 2.80x | $0.90-1.33 |

These are rough estimates. Actual performance depends on model size, batch size, and memory
requirements. When VRAM is the bottleneck (model does not fit), only GPUs with sufficient VRAM
should be considered.

### Reliability Thresholds

Machine reliability is critical, especially for long-running tasks where a crash means restarting
hours of work. Use these minimum thresholds based on estimated task duration:

| Estimated Duration | Min Reliability | Rationale |
| --- | --- | --- |
| Under 1 hour | 0.95 | Short jobs, easy to retry |
| 1-5 hours | 0.98 | Moderate risk from restarts |
| 5-24 hours | 0.995 | High cost of restart |
| Over 24 hours | 0.999 | Only very stable machines |

When filtering offers, always apply the reliability threshold from the table above. A machine with
99.5% reliability has a ~12% chance of failing during a 24-hour job. At 99.9%, the risk drops to
~2.4%.

For tasks over 5 hours, also prefer machines with `max_days` (maximum rental duration) greater than
2x the estimated duration to avoid forced eviction.

### Decision Steps

1. Determine minimum VRAM required for the task
2. Filter offers to those meeting VRAM, RAM, disk, and reliability requirements. Always include the
   default search filters (`compute_cap<1200 cuda_max_good>=12.6`) to block incompatible hardware
3. For all qualifying offers, calculate both `estimated_total_cost` and `estimated_hours` using the
   relative speed table
4. Eliminate offers where `estimated_total_cost` exceeds the task budget
5. Sort remaining offers by `estimated_hours` (fastest first)
6. Find the cost-efficiency sweet spot: starting from the fastest offer, check whether stepping down
   to a slower GPU tier saves significant money relative to the time it adds. Select the fastest GPU
   where stepping down to the next cheaper tier would add substantial wait time (>30 min) for modest
   savings (<$2-3). In other words: pay 3x more for 2x speedup (good trade), but don't pay 20x more
   for 10% speedup (bad trade)
7. Document the rationale in `selection_rationale` including estimated time, estimated cost, and why
   this GPU was chosen over faster and cheaper alternatives

* * *

## Azure ML Pool Selection

The Azure ML provider uses a fixed pool of named VMs declared in `project/azure_vm.json`. There is
no offer marketplace and no price negotiation — every VM in the pool bills at the same hourly rate
(`hourly_cost_usd` per VM entry). The selection algorithm is a deterministic priority walk:

1. Read `project/azure_vm.json`. Each entry has a `priority` (1 = primary), `vm_name`, `region`,
   `workspace`, `resource_group`, `gpu`, `gpu_count`, and `hourly_cost_usd`.
2. Iterate entries in ascending `priority`. For each VM:
   * Check whether `~/.arf-locks/<other_task_id>.lock` exists on the VM via SSH. If a lock owned by
     another task is present, record `lock_held` in `failed_attempts` and continue to the next
     entry.
   * If stopped, issue `az ml compute start`. Wait up to 8 minutes for the VM to reach `Running`.
   * Verify SSH connectivity via the host alias declared in the user's `~/.ssh/config`.
   * On success, write `~/.arf-locks/<task_id>.lock` and return.
3. If every entry is locked or unreachable, write `tasks/<task_id>/intervention/pool_busy.md` and
   exit non-zero.

The whole walk, including `failed_attempts` for any VMs skipped on the way to the chosen one, is
recorded in `machine_log.json`. Stale locks (owned by tasks already in a terminal state) should be
cleared before the walk — see `LESSONS.md` Lesson 8.

* * *

## Mandatory Checkpointing

Jobs estimated at more than 2 hours MUST configure checkpoint saving to protect against instance
eviction, crashes, or SSH disconnection. A full restart of a multi-hour training run wastes both
time and money. See `LESSONS.md` Lesson 8 for the liveness mechanism the orchestrator uses to detect
silent crashes.

### Requirements

* Save a checkpoint every 30 minutes (at minimum) to a known path on the remote machine
* Record the checkpoint path in `machine_log.json` as `checkpoint_path`
* Training scripts should handle `SIGTERM` gracefully: save a final checkpoint before exiting
* On re-provisioning after a failure, the implementation step must check for existing checkpoints
  and resume from the latest one

### Heartbeat

Training scripts should write a heartbeat file every 5 minutes containing the current epoch, step,
loss, and timestamp. Record the path in `machine_log.json` as `heartbeat_path`. The monitoring agent
can poll this file via SSH to detect silent crashes (heartbeat age > 2× the expected interval).

Example heartbeat content:

```json
{"epoch": 3, "step": 1200, "loss": 0.42, "timestamp": "2026-04-05T14:30:00Z"}
```

### Jobs under 2 hours

Checkpointing is optional but recommended. The `checkpoint_path` and `heartbeat_path` fields may be
`null`.

* * *

## Cost Integration

Machine costs must be recorded in two places.

### 1. costs.json

Add a line item to `breakdown` using one of these key formats:

* Vast.ai: `"vast-ai-<gpu-lowercase-hyphenated>"` (e.g., `"vast-ai-h200"`, `"vast-ai-2xh200"`).
* Azure ML: `"azure-ml-<gpu-lowercase-hyphenated>"` (e.g., `"azure-ml-2xh100"`).
* Nebius: `"nebius-<gpu-lowercase-hyphenated>"` (e.g., `"nebius-h200"`, `"nebius-8xh200"`).

```json
{
  "total_cost_usd": 50.39,
  "breakdown": {
    "claude-opus": 1.49,
    "vast-ai-2xh200": 15.49,
    "azure-ml-2xh100": 34.90,
    "nebius-h200": 0.35
  }
}
```

### 2. remote_machines_used.json

Follow the schema in `arf/specifications/task_results_specification.md`:

```json
[
  {
    "provider": "vast_ai",
    "machine_id": "34201117",
    "gpu": "2xH200",
    "gpu_count": 2,
    "ram_gb": 384,
    "duration_hours": 3.24,
    "cost_usd": 15.49
  },
  {
    "provider": "azure_ml",
    "machine_id": "arf-NC80-weu-v1",
    "gpu": "2xH100",
    "gpu_count": 2,
    "ram_gb": 880,
    "duration_hours": 2.50,
    "cost_usd": 34.90
  },
  {
    "provider": "nebius",
    "machine_id": "computeinstance-e00mzby80wfn7y0ahr",
    "gpu": "H200",
    "gpu_count": 1,
    "ram_gb": 200,
    "duration_hours": 0.10,
    "cost_usd": 0.35
  }
]
```

The `machine_id` must match `instance_id` from `machine_log.json`. The `cost_usd` must match
`total_cost_usd` from `machine_log.json`.

* * *

## Verification Rules

### Errors

| Code | Description |
| --- | --- |
| `RM-E001` | `machine_log.json` lists a machine without `destroyed_at` |
| `RM-E002` | Provider API confirms instance is still running/active |
| `RM-E003` | `machine_log.json` is missing or not valid JSON |
| `RM-E004` | A required field is missing from a machine entry |
| `RM-E005` | `instance_id` in `machine_log.json` does not match `machine_id` in `remote_machines_used.json` |
| `RM-E006` | `total_cost_usd` in `machine_log.json` does not match `cost_usd` in `remote_machines_used.json` |
| `RM-E007` | `provider` is not one of the allowed values (`"vast_ai"`, `"azure_ml"`, `"nebius"`) |

### Warnings

| Code | Description |
| --- | --- |
| `RM-W001` | Provider API unreachable (cannot confirm destruction, but `destroyed_at` is present) |
| `RM-W002` | Actual cost exceeds plan estimate by more than 50% |
| `RM-W003` | Machine was running for more than 12 hours |
| `RM-W004` | `selection_rationale` is empty or under 20 characters |
| `RM-W005` | A `failed_attempts` entry is missing required sub-fields |
| `RM-W006` | Job ran more than 2 hours but no `checkpoint_path` is set |
| `RM-W007` | `spec_version` is not `"5"` (legacy entry, treated permissively) |
