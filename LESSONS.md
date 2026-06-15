# Rezolve ARF Lessons

**Version**: 2

A curated index of generalizable lessons accumulated from Rezolve research projects that have been
run on this framework. Each lesson lists: *what went wrong*, *why*, and *how the framework now
mitigates it*. Lessons are referenced from individual skills and verificators so a new project
inherits them by construction.

Read this file before planning a task involving latency benchmarks, GPU provisioning, quantization,
or paired-bootstrap analysis.

* * *

## Lesson 1: Cold-cache measurements are not pairable with warm-cache measurements

**What went wrong** (rail-arf-serving t0004 → t0012, t0013): the project's baseline (t0004) ran 20
cold-cache prompts. Later tasks ran warm-cache measurements. The cross-task deltas were silently
invalid because GPU KV-cache state differed between conditions.

**Why**: vLLM and similar engines warm KV caches on the first few requests after engine launch.
Latency for prompt N=1 is materially different from N=50 on the same engine session.

**Mitigation in the framework**:

* `arf/scripts/protocols/warmup_runner/` ships the protocol shape: 50 discarded warmup requests then
  N=20 measured requests, on the same engine session.
* `meta/asset_types/latency_benchmark_run/specification.md` requires every benchmark asset's
  `details.json` to declare `warmup_requests` and `warmup_corpus_ref`.
* `verify_latency_benchmark_run_warmup` warns whenever either field is missing or
  `warmup_requests < 1`.

* * *

## Lesson 2: Smoke-gate before measurement saves ~10% of VM spend

**What went wrong** (rail-arf-serving t0017, t0018): Eagle3 speculative decoding and FP8/AWQ
quantization tasks each spent ~$49 of VM time before discovering the engine had never launched
correctly. No request had reached the model.

**Why**: quantization checkpoints can fail at engine startup with cryptic errors
(`Unsupported data_type: fp8`, `Cannot find config file for awq`). Without a pre-measurement smoke
check, warmup runs continue against a non-functional engine and the failure is only diagnosed in
post-mortem.

**Mitigation in the framework**:

* `arf/skills/setup-remote-machine/SKILL.md` Phase 4 step 4 ("Engine smoke gate") is **mandatory**
  for any measurement task. One `/health` (or `/version`) call and one minimum-length completion
  must succeed before warmup begins.
* On smoke-gate failure: mark the condition `null`, skip warmup and measurement, proceed directly to
  teardown.
* `machine_log.json` records `smoke_gate_status` (`pass`/`fail`) and the failure reason.

* * *

## Lesson 3: Pre-register a failure-rate rejection threshold before running

**What went wrong** (rail-arf-serving t0015): an SSH tunnel collapse during a high-throughput run
yielded 17/100 successful requests. The paired-bootstrap delta was -7.36 pp BFCL accuracy but the
result was meaningless — the comparison was dominated by failed requests, not the experimental
condition.

**Why**: BCa paired-bootstrap implicitly assumes both arms saw the same prompts under
roughly-equivalent operating conditions. A high failure rate breaks this assumption silently; the
test still produces a confidence interval, which gets quoted downstream as if it were valid.

**Mitigation in the framework**:

* `arf/skills/planning/SKILL.md` requires a `## Rejection Criteria` section in every benchmark
  task's `plan.md`. Default text: **if `successful_requests / total_requests < 0.8` for any
  condition, that condition is null regardless of any measured numbers.**
* Pre-registered before running so the threshold cannot be retroactively loosened.

* * *

## Lesson 4: Capture infrastructure versions at benchmark time, not later

**What went wrong** (rail-arf-serving t0002, audit doc `brainpowa-config-drift-audit-2026-05-13`):
the BFCL gap between two provider endpoints was attributed to the model when it was actually a
container-image drift between dev and prod (vLLM 0.15.0 + CUDA 12.1 + cuDNN 8 vs vLLM 0.19.1 + CUDA
12.8 + cuDNN 9). The audit took weeks because version tags were not captured at the time of the
benchmark.

**Why**: infrastructure (engine version, CUDA, cuDNN, container SHA) changes between benchmark runs.
Without capture-at-time-of-measurement, post-hoc comparison is forensics, not science.

**Mitigation in the framework**:

* `meta/asset_types/latency_benchmark_run/specification.md` requires every benchmark asset's
  `details.json` to declare `engine_version`, `cuda_version`, `cudnn_version`, and (for hosted
  endpoints) `container_image_sha`.
* For remote endpoints, capture via HTTP `/version` or
  `kubectl get deployment ... -o jsonpath='{...image}'` as a pre-flight call.
* For local VMs, capture `nvidia-smi` plus `python -c "import <engine>; print(__version__)"` into
  the asset before measurement begins.

* * *

## Lesson 5: Lock paired-bootstrap seeds and resamples

**What went wrong** (general): without fixed seeds, re-running a paired-bootstrap analysis produces
slightly different confidence intervals, and reviewers cannot reproduce reported numbers.

**Why**: BCa is a Monte Carlo procedure. Without `numpy.random.default_rng(seed)`, results drift.

**Mitigation in the framework**:

* `arf/scripts/stats/bootstrap_compare/constants.py` locks `BOOTSTRAP_SEED=12345`,
  `BOOTSTRAP_ITERATIONS=5000`, `PERMUTATION_ITERATIONS=10000`, `CONFIDENCE_LEVEL=0.95`.
* These values are intentionally cross-project constants — keep them stable so results from
  different Rezolve projects remain comparable.

* * *

## Lesson 6: Frozen-baseline contract

**What went wrong** (rail-arf-serving t0014 → t0015 → t0017 → t0018): a pattern of "clone the
baseline config, add one flag, run paired sweep" worked great until someone modified the baseline
asset. All downstream ablations then silently computed wrong deltas against a moving target.

**Why**: ARF's task-isolation rules prevent edits to *other* task folders, but they do not prevent a
later task from registering a new asset under the same name in its own folder. Aggregators apply the
corrections overlay but downstream tasks may pin the wrong version.

**Mitigation in the framework**:

* Baseline configs (e.g., `vllm_config`, `model_config`) used by multiple downstream tasks should be
  named with a `_FROZEN` suffix and a version (`_v1`, `_v2`). Downstream ablations reference the
  baseline by ID **and** a git-commit SHA at which the baseline was last validated.
* If a baseline needs revision, register a new `_v2` asset rather than mutating `_v1`.

* * *

## Lesson 7: Pre-validate quantization checkpoints offline

**What went wrong** (rail-arf-serving t0018): both FP8 W8A8 and AWQ candidates failed at engine
launch. The checkpoints existed on HuggingFace but were either the wrong dtype (FP8 enum not
accepted by `compressed-tensors` adapter) or missing AWQ-specific tensors (`qweight`, `qzeros`,
`scales`).

**Why**: quantization format compatibility is implicit. There is no `pip check` equivalent for
checkpoint × engine compatibility — it fails at runtime, after VM provisioning.

**Mitigation in the framework**:

* Any task plan that includes quantization must list a `## Checkpoint Validation` step that runs
  **before** VM provisioning. The step downloads `config.json` and inspects safetensors shard keys
  for the expected quantization-specific tensors. Document the validated HuggingFace model IDs and
  commit SHAs in the plan.

* * *

## Lesson 8: Orchestrator must own step liveness — fire-and-forget handoffs cause idle billing

**What went wrong** (rail-arf-serving, GPU benchmark task): a subagent driving an `implementation`
step finished its setup work, spawned a background poller watching for an engine-ready sentinel on
the GPU VM, and returned control with a scheduled wakeup registered. Neither the wakeup nor the
poller actually drove the benchmark when the engine became ready. The VM kept billing for ~9 hours
before a human caught it, costing ~$130 of unbudgeted spend.

**Why**: ARF had no notion of "who is currently driving this step". `step_tracker.json` recorded
`status: "in_progress"` but nothing tracked an owner or a heartbeat, so a subagent could legally
exit while leaving work in a background poller. There was no verificator to flag the absence of
forward progress and no orchestrator-side liveness scan to detect the gap before re-delegating. The
failure compounds because the wakeup mechanism is itself fragile: a re-delegated subagent that hits
a usage cap or simply does not fire leaves the parent task silently stalled with the VM still
running.

**Mitigation in the framework**:

* Step-tracker v2 liveness fields (`current_owner`, `last_heartbeat_at`,
  `heartbeat_interval_seconds`, `expected_completion_at`) are required on every `in_progress` step —
  see `arf/specifications/step_tracker_specification.md`.
* `arf/scripts/utils/heartbeat.py` (`start_step`, `write_heartbeat`, `pause_step`, `complete_step`)
  is the single canonical way for a step owner to maintain liveness.
* `arf/scripts/verificators/verify_step_liveness.py` flags stale heartbeats (`ST-E007` when a live
  VM is still billing, `ST-W005`/`ST-W006` otherwise) and unsafe pauses (`ST-E008`).
* `arf/skills/execute-task/SKILL.md` Phase −1 runs `verify_step_liveness --all` at the start of
  every wakeup; `arf/skills/implementation/SKILL.md` forbids fire-and-forget background pollers.
* For long external waits, a step may only `pause_waiting` when the VM carries the idle
  dead-man's-switch watchdog (`arf/scripts/utils/idle_watchdog.sh` +
  `arf/scripts/utils/watchdog_provisioning.py`) — the watchdog, not the orchestrator, is what
  guarantees a missed wakeup cannot leave the box billing. The `/diagnose-stuck-step` skill produces
  a structured recovery report for any flagged step.

* * *

## Adding new lessons

When a Rezolve research project produces a generalizable lesson:

1. Add a new `## Lesson N: <one-line headline>` section to this file.
2. Use the four-part structure: *What went wrong* (with task/project reference), *Why*, *Mitigation
   in the framework*.
3. Implement the mitigation as a default in the relevant skill, asset spec, or verificator. A lesson
   without a corresponding default is just a complaint.
4. Increment the file's `**Version**` line at the top.
