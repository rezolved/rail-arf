# Rezolve ARF Lessons

**Version**: 9

A curated index of generalizable lessons accumulated from Rezolve research projects that have been
run on this framework. Each lesson lists: *what went wrong*, *why*, and *how the framework now
mitigates it*. Lessons are referenced from individual skills and verificators so a new project
inherits them by construction.

Read this file before planning a task involving latency benchmarks, GPU provisioning, quantization,
paired-bootstrap analysis, or any benchmark run on a fine-tuned model.

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
* `arf/skills/execute-task/SKILL.md` Phase −0.5 forbids ending an invocation with any step left
  `in_progress` unless a `ScheduleWakeup` is registered — ending a turn to await a notification that
  no component will send is the same fire-and-forget bug wearing a different hat.
* `arf/scripts/hooks/verify_logs_on_stop.py`, the `Stop` hook registered in `.claude/settings.json`,
  is what makes that Phase −0.5 rule more than prose: it blocks the first attempt to end a turn
  while a live task still holds an `in_progress` step. See the fourth follow-up for what it does and
  does not guarantee.
* `/setup-remote-machine` Phase 3 installs the watchdog on **every** GPU machine, not only on ones
  that will pause, and records `watchdog_active` in `machine_log.json` only after confirming a PID.
  `verify_step_liveness` flags an unprotected live machine as `ST-W008`.

**Follow-up (2026-07-31)**: an audit prompted by a task sitting stale for ~8 hours found that the
detection half of this mitigation had never worked. `verify_step_liveness` and
`/diagnose-stuck-step` both identified a live VM by `actual_status == "running"` — a Vast.ai
provider-API field that is never written into `machine_log.json`. Every machine therefore read as
destroyed: `ST-E007` silently degraded to the `ST-W005` warning, the verificator exited `0`, and the
diagnostic skill concluded "no VM" without probing. Liveness is now defined as non-empty
`instance_id` and absent `destroyed_at`, the same signal `RM-E001` already used.

The generalizable part: a detector keyed on a field the schema does not guarantee fails silently,
and it fails in the safe-looking direction. Two habits prevent it — pin a detector to a field its
own specification requires, and build test fixtures from the real schema instead of letting the test
invent a field the producer never writes. The unit test here passed for months because it wrote
`actual_status` into its own fixture.

**Second follow-up (2026-07-31)**: fixing the detector was not enough, because nothing fed it.
`prestep.py` marked every step `in_progress` writing only `status`, `started_at`, and `log_file` —
never the liveness fields — and nothing stamped `spec_version` on a tracker. So `ST-E007`,
`ST-W005`, and `ST-W006`, which all need `last_heartbeat_at`, and `ST-E009`, which needs
`spec_version`, were dormant across every task in the project. `heartbeat.py` implemented the
contract correctly and completely, and no step-executor ever called it. A task then sat silent for
14 hours holding an H100 while the liveness scan reported clean — a scan with no data has no
findings.

`prestep` now arms all four fields and stamps `spec_version`; `poststep` and `skip_step` finalize.
Arming happens in the lifecycle, not in the step's own code, so a step is monitorable from the
moment it starts whether or not its owner ever heartbeats again.

The generalizable part: **a contract that depends on every participant remembering to call an API is
not in force.** When adding a rule that reads a field, check who writes it, and put the write
somewhere mandatory rather than somewhere polite. The tell is cheap to look for — grep for a
*producer* of the field, not just a consumer. Here the producer count was zero, and both the
specification and a fully passing test suite described a system that did not exist.

**Third follow-up (2026-07-31)**: an audit of the finished liveness stack found the sanctioned
release path had no failure branch. `pause_step` recorded only a prose `resume_sentinel`, and the
resume instruction was "re-check the sentinel and either finish or pause again". A remote job that
dies mid-wait never produces its sentinel, so resume re-pauses — every wakeup, indefinitely. The
watchdog stops the VM an hour later, which bounds the money and hides the symptom: the task now
reads as "waiting" forever on a job that no longer exists, and only a human reading a log finds out.

A pause now records a `liveness_probe` (a command that exits `0` while the work runs), and resume is
a three-way decision — finished, alive, or **dead** — driven by `arf/scripts/utils/resume_check.py`
rather than by a skill's judgement. `pause_count` is the belt to that brace: it needs no SSH, so it
catches the pause that recorded no probe at all, and `ST-E010` fires past 12 re-pauses.

The generalizable part: **a wait state needs a way to fail, not just a way to continue.** Any
"check, then sleep again" loop where the check can never turn true is a hang wearing the costume of
patience. When adding one, ask what makes it stop being true — and if the answer is only "the thing
we are waiting for arrives", add the branch for the thing that never arrives. A bounded retry count
is the cheap version, and it belongs there even when a smarter check exists.

**Fourth follow-up (2026-08-03)**: with the detector fixed, its data armed, and a pause that can
fail, a step-executor left an SFT training job running, returned control expecting a self-made
background poller to wake it, and the step sat `in_progress` and unattended for ~20 hours. Azure's
idle shutdown stopped the billing; nothing else would have. The training had in fact finished
cleanly — what was lost was a night of wall-clock, not data.

Every part of the liveness stack ran correctly and none of it helped, because all of it only
executes **when something invokes the orchestrator**, and nothing does. `verify_step_liveness --all`
is run by `execute-task` Phase −1, which requires a wakeup that never came. The rule against this
existed, in prose, in two skills the executor was following: `implementation` v12 forbids
fire-and-forget background pollers, and `execute-task` v26 forbids ending an invocation with an
`in_progress` step and no `ScheduleWakeup`. Both were live. Both were violated.

The mitigation is the `Stop` hook, `arf/scripts/hooks/verify_logs_on_stop.py` (PR #91): it runs on
every attempt to end a turn — the one moment guaranteed to happen — and exits `2` when a live task
still carries an `in_progress` step, returning the two legal exits to the agent. Recorded at the
same time, not left for a later audit to find: it is **one forced reminder per turn, not enforcement
by construction.** `stop_hook_active` short-circuits the check on the repeat stop, so an agent can
be blocked once, change nothing, and leave; and the hook cannot see whether a wakeup was actually
registered, because schedules live in session memory rather than on disk. It closes the silent exit,
not every exit. Nothing yet survives the death of the Claude session itself — there, the VM watchdog
and the provider's idle shutdown are still the only floors.

The generalizable part, and the reason this is a fourth follow-up rather than a new lesson: **this
is the same failure shape for the third time.** A rule that lives only in text addressed to an agent
is not in force — the same sentence as the second follow-up, applied to a rule instead of a field.
The tell is cheap to check at the moment of writing, and cheaper than the audit that finds it later:
ask who *executes* the new rule, not who is *told* it. If the answer is "the agent, if it
remembers", put it on a path the lifecycle runs — a hook, a `prestep`, a provisioning script — even
when the prose version already exists and reads convincingly.

* * *

## Lesson 9: Fine-tuned-model benchmarks need a full side-by-side report, not a metrics table

**What went wrong** (rail-arf-finetuning t0017): a task that benchmarked a fine-tuned (FT) model
produced an HTML report that omitted the benchmark conversation itself. The report showed scores
without the dialog fed to the model, the model's actual answers, or the judge's reasoning. Reviewers
could not see *why* a case passed or failed, nor isolate where the FT model regressed against base —
so the report had to be regenerated.

**Why**: aggregate accuracy and pass/fail counts hide the behavior that fine-tuning actually
changed. Understanding an FT result requires reading, per case, the exact conversation prefix, both
the base and FT answers, and the judge verdict plus reasoning — side by side — and filtering to the
cases where FT did better or worse than base. A bare metrics table cannot answer "what did
fine-tuning change, and was it for the better?", which is the entire point of the comparison.

**Mitigation in the framework**:

* Every task that runs a benchmark on a fine-tuned model must produce a
  `benchmark_comparison_report` asset (`meta/asset_types/benchmark_comparison_report/`). Per case it
  captures: (1) the benchmark conversation prefix (the full dialog fed to the model), (2) both model
  answers (base **and** FT), and (3) the judge verdict + full reasoning for each. The asset's
  `report.html` is a self-contained side-by-side rendering with summary cards (per-model accuracy,
  improvement %, improved/declined counts) and tabbed/filterable views — Improved (base failed, FT
  passed), Declined (base passed, FT failed), Both Passed, Both Failed, All — so reviewers can
  isolate regressions and gains.
* The structured `cases.jsonl` is the source of truth and the verifiable artifact;
  `meta/asset_types/benchmark_comparison_report/generate_report.py` renders `details.json` and
  `report.html` from it. The visual reference design is
  `real-repos/rail-benchmarks/azure-ai-foundry/clarification-benchmarks/generate_comparison_report.py`.
* `meta/asset_types/benchmark_comparison_report/verificator.py` enforces completeness: it fails
  (`BCR-E008`/`BCR-E009`/`BCR-E010`) when any case lacks the conversation prefix, both answers, or
  both judge reasonings — the exact t0017 gap — and checks bucket/summary consistency.
* `arf/skills/planning/SKILL.md` requires this asset as a planned deliverable for any FT-model
  benchmark task, pre-registered before running.
* The `experiment-run`, `comparative-analysis`, `baseline-evaluation`, and `build-model` task-type
  instruction files (`meta/task_types/*/instruction.md`) carry the requirement in their
  Implementation Guidelines and Verification Additions so FT benchmark plans inherit it.

* * *

## Lesson 10: Azure ML VM persistent storage requires an explicit symlink — `/mnt` is ephemeral

**What went wrong** (rail-arf-finetuning t0007): `train_supervisor.sh` wrote training checkpoints
and the final SFT-LoRA adapter to `/mnt/cache/persist/runs/sft_v1/adapter/` using `mkdir -p`. The
directory was created on the ephemeral `/mnt` temp disk, not on the Azure Files share, because the
symlink `/mnt/cache/persist → <azure-files-mount>` was never created. When the VM was stopped after
t0007, `/mnt` was wiped and the adapter was lost. It survived only because it had been DVC-pushed to
blob storage before the VM stopped.

**Why**: Azure ML VMs mount an Azure Files SMB share into the container at a deep path under
`/mnt/batch/tasks/shared/LS_root/mounts/clusters/<vm-name>/code/`. This path survives VM stop and
restart. The `/mnt` directory itself (a temp disk) is ephemeral and wiped on every stop. Any
`mkdir -p /mnt/cache/persist` call silently creates a directory on the ephemeral disk rather than
pointing at the persistent share, and there is no error — the path just vanishes on the next stop.

**Mitigation in the framework**:

* **Confirmed persistent storage path pattern for Rezolve Azure ML VMs**:

  ```
  /mnt/batch/tasks/shared/LS_root/mounts/clusters/<vm-name>/code/
  ```

  Example for FT-NC80-v1: `/mnt/batch/tasks/shared/LS_root/mounts/clusters/ft-nc80-v1/code/`

  100 TB quota; ~4.4 TB used as of 2026-06. Shared workspace-level share — all VMs in the
  `finetuning-workspace` see the same data under their own `clusters/<vm-name>/code/` sub-path.

* **Every training task must verify the symlink resolves to the real mount before writing any
  checkpoint**. `/mnt/cache/persist` is a symlink, but a symlink pointing to an unmounted or wrong
  target is silent data loss. Add this to the Setup Machine step:

  ```bash
  # Verify the symlink target is the actual Azure Files mount (not ephemeral /mnt)
  readlink -f /mnt/cache/persist
  # Expected: /mnt/batch/tasks/shared/LS_root/mounts/clusters/<vm-name>/code

  # Confirm mounted and writable
  df -h /mnt/cache/persist   # must show the Azure Files share, not tmpfs
  touch /mnt/cache/persist/.write_test && rm /mnt/cache/persist/.write_test

  # If readlink resolves to an ephemeral /mnt path, fix the symlink:
  ln -sfn /mnt/batch/tasks/shared/LS_root/mounts/clusters/$(hostname)/code \
      /mnt/cache/persist
  ```

* Write **all** checkpoints, intermediate artifacts, and final adapters to paths under
  `/mnt/cache/persist/`. Write nothing training-related directly under `/mnt/` — it is ephemeral.

* DVC-push each adapter **immediately after it completes** (before the VM is stopped or the next
  training step begins) as a second safety net. DVC blob storage is the recovery path if the Azure
  Files share itself is unavailable.

* **The check runs in the lifecycle, not in a skill's prose.**
  `arf/scripts/utils/remote_preflight.sh` performs exactly the block above — resolve, repair with
  `ln -sfn`, verify writable — and `azure_ml_vm.acquire()` pipes it over SSH before it places the
  task lock. A VM whose persistent mount is missing or unwritable is rejected with
  `failure_phase: "preflight"` and the provisioner moves to the next pool entry, so no job ever
  starts on a box that will silently eat its checkpoints.

**Follow-up (2026-07-31)**: this mitigation previously read "`setup-remote-machine/SKILL.md` and any
implementation skill that starts GPU work *should* include the symlink verification". It never did —
a `grep` for `readlink` across `arf/` returned nothing at all. The lesson had been written down,
declared mitigated, and implemented nowhere, which is the same failure Lesson 8's second follow-up
describes: a contract nobody executes is not in force. The fix is not a better-worded instruction;
it is a script on a mandatory path.

* * *

## Lesson 11: `tmux` alone does not survive SSH disconnection without `loginctl enable-linger`

**What went wrong** (rail-arf-finetuning t0021): a DPO training job launched inside a named `tmux`
session on FT-NC80-v1 was killed ~24 minutes in, at step 27/540, with exit code 143 (SIGTERM). No
OOM, no GPU fault, no spot preemption (the VM has no priority/spot tier). `journalctl` showed
`systemd-logind: Removed session N` at the exact second of the crash: when the SSH session that
launched `tmux` ended, `systemd-logind` tore down that user's entire session scope — including the
detached `tmux` server and everything running inside it — because `azureuser` had `Linger=no` (the
Ubuntu default).

**Why**: `tmux new-session -d` detaches the session from the *terminal*, but on systemd-managed
Linux hosts the processes still belong to the *login session's cgroup scope* unless lingering is
enabled. `Linger=no` means `systemd-logind` cleans up that scope (SIGTERM then SIGKILL to everything
in it) as soon as the last session for that user closes — even though `tmux` itself keeps running as
a server, its child processes get torn down. This is easy to miss because `tmux has-session` right
after disconnecting still reports the session as alive for a window, and the training log looks
completely normal (no error) right up to the kill.

**Mitigation in the framework**:

* `arf/scripts/utils/remote_preflight.sh` runs `loginctl enable-linger` for the SSH user and
  **verifies** `Linger=yes` before any job is launched. `azure_ml_vm.acquire()` runs it over SSH
  before placing the task lock, so lingering is a property of every acquired machine rather than a
  step someone has to remember. A box where lingering cannot be enabled is rejected with
  `failure_phase: "preflight"`.
* `arf/skills/setup-remote-machine/SKILL.md`'s "Running long jobs" section explains why both
  guarantees exist and how to re-check them by hand — `tmux` and lingering are both required,
  neither alone is sufficient.
* When diagnosing an unexplained mid-job SIGTERM (rc=143) with no OOM/GPU/preemption evidence, check
  `journalctl -u user@$(id -u).service` (or `journalctl | grep logind`) for a `Removed session`
  entry at the crash timestamp before assuming spot eviction or a code bug.
* Training/eval scripts that write periodic checkpoints (as Lesson 10 already requires for the
  persistent-storage path) limit the blast radius of this failure mode to the checkpoint interval,
  not the whole run — keep checkpoint intervals short relative to expected job duration.

* * *

## Adding new lessons

When a Rezolve research project produces a generalizable lesson:

1. Add a new `## Lesson N: <one-line headline>` section to this file.
2. Use the four-part structure: *What went wrong* (with task/project reference), *Why*, *Mitigation
   in the framework*.
3. Implement the mitigation as a default in the relevant skill, asset spec, or verificator. A lesson
   without a corresponding default is just a complaint.
4. Increment the file's `**Version**` line at the top.
