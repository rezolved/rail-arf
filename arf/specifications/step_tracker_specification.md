# Step Tracker Specification

**Version**: 7

* * *

## Purpose

This specification defines the format and requirements for the `step_tracker.json` file that tracks
task execution progress and step liveness.

**Producer**: Task agents and subagents (create and update during execution); the heartbeat helper
at `arf/scripts/utils/heartbeat.py` writes the liveness fields.

**Consumers**:

* **Task subagents** — read to determine which steps are complete
* **Orchestrator** — reads liveness fields to detect ghosted or pathologically slow steps before
  delegating more work
* **Verificator scripts** — validate step completion, log coverage, and step liveness
  (`verify_step_liveness.py`)
* **Diagnostic skills** — read liveness fields to scope the investigation (`/diagnose-stuck-step`)
* **Human reviewers** — monitor task progress at checkpoints
* **Aggregator scripts** — collect execution metrics across tasks (a future
  `aggregate_step_durations` will mine `actual_duration_seconds` to flag outlier step kinds)

* * *

## File Location

```text
tasks/<task_id>/step_tracker.json
```

One file per task. Created when the task starts; updated as each step progresses.

* * *

## Top-Level Fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `spec_version` | string | yes | Specification version (e.g., `"6"`) |
| `task_id` | string | yes | Must match the task folder name |
| `steps` | list[Step] | yes | Ordered list of task steps |

The `spec_version` field is required starting at v2. v1 trackers without it are treated as
backward-compatible and silently skipped by the liveness verificator.

`prestep` stamps `spec_version` on the tracker whenever it marks a step `in_progress`, so a tracker
becomes v2+ the first time a step starts under this version. A tracker mid-flight from an older
version is upgraded in place: its already-completed steps keep whatever fields they had, and only
the newly-started step carries the liveness fields. Nothing rewrites history.

* * *

## Step Object

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `step` | int | yes | Step number (1-indexed, sequential) |
| `name` | string | yes | Short human-readable name |
| `description` | string | yes | What this step accomplishes |
| `status` | string | yes | One of: `"pending"`, `"in_progress"`, `"completed"`, `"failed"`, `"skipped"`, `"blocked_intervention"`, `"paused_waiting"` |
| `started_at` | string \| null | yes | ISO 8601 UTC timestamp, `null` when pending |
| `completed_at` | string \| null | yes | ISO 8601 UTC timestamp, `null` when not finished |
| `log_file` | string \| null | no | Relative path to step log folder in `logs/steps/` |
| `current_owner` | string \| null | yes when in_progress | Identifier of the agent or subagent driving this step right now; `null` when not `in_progress` |
| `last_heartbeat_at` | string \| null | yes when in_progress | ISO 8601 UTC timestamp updated by the owner every `heartbeat_interval_seconds` |
| `heartbeat_interval_seconds` | int \| null | yes when in_progress | The owner's promised heartbeat cadence (e.g., 300 for 5-minute heartbeats) |
| `expected_completion_at` | string \| null | yes when in_progress | ISO 8601 UTC best-effort estimate set at step start |
| `actual_duration_seconds` | int \| null | yes when completed | Wall-clock duration in seconds, written when the step transitions to `completed`, `failed`, or `skipped`. `null` when the step never started (e.g. skipped from `pending`) — a step that never ran has no duration, and `0` would read as "ran instantly" |
| `resume_sentinel` | string \| null | yes when paused_waiting | What the step is waiting on and how to re-check it on resume (e.g., benchmark output path on the VM) |
| `paused_at` | string \| null | yes when paused_waiting | ISO 8601 UTC timestamp the step entered `paused_waiting` |
| `resume_after` | string \| null | yes when paused_waiting | ISO 8601 UTC earliest time the orchestrator should attempt resume |
| `watchdog_active` | bool \| null | yes when paused_waiting | `true` iff the VM carries an idle dead-man's-switch watchdog; pausing requires this |
| `liveness_probe` | string \| null | recommended when paused_waiting | A shell command that exits `0` while the remote work is still running (e.g. `ssh HOST tmux has-session -t train`). Run by `arf/scripts/utils/resume_check.py` at resume to tell a live job from a dead one |
| `pause_count` | int \| null | yes when paused_waiting | How many times this step has entered `paused_waiting`. Incremented by `pause_step`; a step that keeps re-pausing without converging is flagged `ST-E010` |

### Status Values

* `"pending"` — step has not started yet
* `"in_progress"` — step is currently executing, has a non-null `current_owner` and fresh
  `last_heartbeat_at`
* `"completed"` — step finished successfully
* `"failed"` — step failed (see step log for details)
* `"skipped"` — step was intentionally skipped (see step log for reason)
* `"blocked_intervention"` — step paused waiting for a human; an `intervention/` file describes what
  is needed
* `"paused_waiting"` — step deliberately paused on a long external wait (e.g., a multi-hour GPU
  benchmark) so the session can end instead of babysitting a warm context. Has a non-null
  `resume_sentinel`, `resume_after`, and `watchdog_active`; `current_owner` is `null`. See "Paused
  steps and resume-from-files" below

### The `log_file` Field

When a step reaches `"completed"`, `"failed"`, or `"skipped"` status, the agent must set `log_file`
to the relative path of the corresponding step log folder (e.g.,
`"logs/steps/005_research-internet/"`). This creates a verifiable link between the tracker and the
actual log folder.

### Liveness Fields

The four liveness fields (`current_owner`, `last_heartbeat_at`, `heartbeat_interval_seconds`,
`expected_completion_at`) together let the framework detect two distinct failure modes:

* **Ghosted step**: `last_heartbeat_at` is older than `heartbeat_interval_seconds * 3` — the owner
  silently stopped driving the step. Flagged by `verify_step_liveness.py` as `ST-W005` (no live VM)
  or `ST-E007` (live VM still burning money).
* **Pathologically slow step**: `last_heartbeat_at` is fresh but the elapsed wall-clock exceeds
  `expected_completion_at` by a configurable factor. Flagged as `ST-W006`. Catches "alive but
  stuck-in-a-loop" cases like verifiers running 40× or post-processing scripts that should take
  minutes taking hours.

A subagent that returns control without transitioning out of `in_progress` is a framework bug: drive
the step to a terminal state synchronously, transition to `blocked_intervention` with an explicit
`intervention/<n>_<reason>.md` file, or — for a long external wait on a watchdog-protected VM —
transition to `paused_waiting` (see below). A fire-and-forget background poller that leaves the step
`in_progress` with no owner is forbidden.

The same rule binds the orchestrator: no session may end while any step is still `in_progress`
unless a `ScheduleWakeup` is registered to re-enter it. Nothing polls a task on its own — an
orchestrator runs only when an event wakes it, and work running on a remote host (a `tmux` session
reached over SSH, a detached training job) emits no such event. Ending a turn to "wait for a
notification" that no component will send leaves the step `in_progress` and unattended until a human
intervenes, with the VM billing throughout. `paused_waiting` is the only sanctioned way to release a
session mid-step, and it requires a watchdog.

**Enforced by** `arf/scripts/hooks/verify_logs_on_stop.py`, registered as the `Stop` hook in
`.claude/settings.json`. It runs on every attempt to end a turn and exits `2` — which blocks the
stop and returns its message to the agent — when any task with `status: "in_progress"` still carries
an `in_progress` step. `paused_waiting` passes; so does a repeat stop carrying `stop_hook_active`,
which is what keeps a turn from becoming unstoppable. The gate on the task's own status is
deliberate: a completed task holding a stale `in_progress` step must not block every future stop in
the repository.

What ships is therefore one forced reminder per turn, not enforcement by construction. Because
`stop_hook_active` short-circuits the check, an agent can be blocked once, change nothing, stop
again, and leave; and because the hook cannot verify that a wakeup was actually registered —
schedules live in session memory, not on disk — a `paused_waiting` step with no wakeup passes. The
hook closes the silent exit, not every exit. The check is also repo-wide: a blocked session may be
uninvolved with the step named, so the block message warns against driving a step the session does
not own.

### Paused steps and resume-from-files

`paused_waiting` lets a long GPU wait release its session instead of holding a warm context idle for
hours (the token cost the watchdog was built to remove). The owner calls
`arf.scripts.utils.heartbeat.pause_step` (or `heartbeat pause` on the CLI), which records
`resume_sentinel`, `paused_at`, `resume_after`, and `watchdog_active`, clears `current_owner`, and
returns. A later `execute-task` wakeup re-reads the tracker, and once `now >= resume_after`
re-dispatches the step's skill to re-check the sentinel and either complete or re-pause.

Pausing is safe **only** when the VM carries an idle dead-man's-switch watchdog — otherwise a missed
resume leaves the box billing, which is the banned fire-and-forget pattern (`LESSONS.md` Lesson 8).
`verify_step_liveness` enforces this: a `paused_waiting` step with `watchdog_active != true` is
flagged `ST-E008` (error). A `paused_waiting` step with `watchdog_active == true` is a deliberate,
safe wait and is never treated as ghosted, regardless of heartbeat age.

#### A pause must be able to end in failure

The watchdog bounds the *money* a pause can lose; it does nothing for the *task*. A training job
killed at 03:00 leaves the sentinel absent forever, so a resume that only re-checks the sentinel
re-pauses, and re-pauses, and never tells anyone the run died. That is a hang, and the watchdog
stopping the VM an hour later makes it quieter, not shorter.

Every pause therefore records a `liveness_probe`: a shell command that exits `0` while the remote
work is still running. Resume is a three-way decision, not a two-way one:

1. **Sentinel present** — the work finished. Drive the step to a terminal state.
2. **Sentinel absent, probe exits `0`** — the work is still running. Re-pause with a new
   `resume_after`.
3. **Sentinel absent, probe exits non-zero** — the work died. Do **not** re-pause: transition the
   step to `failed` or `blocked_intervention` with an intervention file naming the probe, its exit
   code, and where its log lives.

`arf/scripts/utils/resume_check.py` runs the recorded probe and reports which branch applies, so the
decision is a script's output rather than a skill's judgement. A pause with no `liveness_probe`
falls back to the old two-way behavior and is flagged `ST-W009`.

`pause_count` is the belt to the probe's braces: it costs no SSH and catches the case the probe
cannot, including a step paused by a skill that never recorded a probe at all. `pause_step`
increments it on every pause, and `verify_step_liveness` raises `ST-E010` past the cap (default 12).
A step that has re-paused a dozen times is not waiting — it is stuck, and a human should look.

### The watchdog is required for every live machine

Every failure mode above — ghosted owner, unmonitorable step, orchestrator session that ended
without a `ScheduleWakeup` — ends the same way: nothing is driving the task and the GPU keeps
billing. Detection helps only when someone is awake to read it, so detection alone cannot bound the
loss. The idle watchdog can, because it runs on the machine itself and depends on no agent waking
up.

`/setup-remote-machine` therefore installs the watchdog on every GPU machine during provisioning and
records `watchdog_active` in `machine_log.json` (see
`arf/specifications/remote_machines_specification.md`). `verify_step_liveness` flags a live machine
without it as `ST-W008`. This caps the blast radius of every liveness gap at the watchdog's idle
timeout instead of at however long it takes a human to notice.

### Who writes the liveness fields

The liveness fields are written by the step lifecycle itself, not by the step's own good intentions.
A contract that depends on every executor remembering to call an API is a contract that is not in
force:

* **`prestep`** writes all four fields when it marks the step `in_progress`, and stamps
  `spec_version` on the tracker. This is what arms detection — a step is monitorable from the moment
  it starts, whether or not its owner ever heartbeats again.
* **The owner** refreshes `last_heartbeat_at` with `write_heartbeat` while it works.
* **`poststep`** and **`skip_step`** finalize: they clear `current_owner` and write
  `actual_duration_seconds` when the step reaches a terminal state.

`prestep`, `poststep`, and `skip_step` delegate these writes to `arf/scripts/utils/heartbeat.py`
rather than reimplementing them, so there is exactly one place that knows the field semantics.

#### The `current_owner` format

`current_owner` is `<driver>/<step_id>` — the thing currently driving the step, then the step it is
driving. `prestep` writes `step-executor/<step_id>` by default; a skill that takes over passes its
own name, e.g. `setup-remote-machine/setup-machines`.

The format matters because `write_heartbeat` overwrites `current_owner` on every refresh. A refresh
that passes a bare step id erases which agent is actually driving — exactly the fact a stuck-step
diagnosis needs first.

### Heartbeat Cadence

Choose `heartbeat_interval_seconds` based on the step's expected wall-clock duration:

| Expected duration | Recommended interval |
| --- | --- |
| < 5 min | not required |
| 5-30 min | 60 s |
| 30 min - 4 h | 300 s (5 min) |
| > 4 h | 600 s (10 min) |

The owner calls `arf.scripts.utils.heartbeat.write_heartbeat` from a `while True` loop or as part of
its main processing loop. CLI form:
`python -m arf.scripts.utils.heartbeat write <task_id> <step_number> <owner>`.

#### Why the prestep default is looser than this table

`prestep` defaults `heartbeat_interval_seconds` to **1800 s** and the expected duration to **3600
s** when the caller does not pass them. Both are deliberately looser than the table above, because
they serve a different purpose: the table is a *target* for an owner that heartbeats, while the
default is a *floor for detection* covering owners that do not heartbeat yet.

The stale threshold is `interval × 3`, so the default declares a step dead after 90 minutes of
silence. That is the trade-off in one line: a step genuinely working for over 90 minutes without
heartbeating is flagged, and a step that died is caught in 90 minutes instead of whenever a human
happens to look. The second failure costs GPU-hours; the first costs one diagnostic the coordinator
resolves by checking and moving on.

Pass explicit values whenever the step's duration is known — a long training step should declare a
realistic expected duration and heartbeat on the table's cadence, rather than inherit a default
tuned for the unknown case.

* * *

## Example

```json
{
  "spec_version": "6",
  "task_id": "0008-baseline-sentiment-classifier",
  "steps": [
    {
      "step": 1,
      "name": "create-branch",
      "description": "Create task branch from main.",
      "status": "completed",
      "started_at": "2026-03-30T08:50:00Z",
      "completed_at": "2026-03-30T08:50:01Z",
      "log_file": "logs/steps/001_create-branch/",
      "current_owner": null,
      "last_heartbeat_at": null,
      "heartbeat_interval_seconds": null,
      "expected_completion_at": null,
      "actual_duration_seconds": 1
    },
    {
      "step": 2,
      "name": "research-papers",
      "description": "Review papers in the corpus relevant to baseline approaches.",
      "status": "completed",
      "started_at": "2026-03-30T09:00:00Z",
      "completed_at": "2026-03-30T09:24:00Z",
      "log_file": "logs/steps/002_research-papers/",
      "current_owner": null,
      "last_heartbeat_at": null,
      "heartbeat_interval_seconds": null,
      "expected_completion_at": null,
      "actual_duration_seconds": 1440
    },
    {
      "step": 3,
      "name": "implementation",
      "description": "Run the baseline classification experiment on the H100 VM.",
      "status": "in_progress",
      "started_at": "2026-03-30T11:00:00Z",
      "completed_at": null,
      "log_file": null,
      "current_owner": "execute-task/implementation-subagent",
      "last_heartbeat_at": "2026-03-30T11:55:00Z",
      "heartbeat_interval_seconds": 300,
      "expected_completion_at": "2026-03-30T13:00:00Z",
      "actual_duration_seconds": null
    },
    {
      "step": 4,
      "name": "teardown",
      "description": "Tear down remote machines and release the lock.",
      "status": "pending",
      "started_at": null,
      "completed_at": null,
      "log_file": null,
      "current_owner": null,
      "last_heartbeat_at": null,
      "heartbeat_interval_seconds": null,
      "expected_completion_at": null,
      "actual_duration_seconds": null
    }
  ]
}
```

* * *

## Verification Rules

### Errors

The `Enforced by` column names the script that actually implements each rule. A code marked **not
enforced** is described here but implemented nowhere: treat it as a documented intention, not as a
check that runs. Never cite a not-enforced code as evidence a tracker is valid — the first liveness
bug survived for months precisely because the spec described checks the code did not perform.

| Code | Description | Enforced by |
| --- | --- | --- |
| `ST-E001` | `step_tracker.json` does not exist or is not valid JSON | **not enforced** |
| `ST-E002` | `task_id` does not match the task folder name | **not enforced** |
| `ST-E003` | `steps` is missing or not a list | **not enforced** |
| `ST-E004` | A step is missing required fields (`step`, `name`, `description`, `status`) | **not enforced** |
| `ST-E005` | Step numbers are not sequential starting from 1 | **not enforced** |
| `ST-E006` | `status` is not one of the allowed values | **not enforced** |
| `ST-E007` | An `in_progress` step has a stale heartbeat AND a live VM is provisioned for the task (idle billing risk) | `verify_step_liveness.py` |
| `ST-E008` | A `paused_waiting` step has `watchdog_active` set to anything other than `true` — an unprotected pause | `verify_step_liveness.py` |
| `ST-E009` | An `in_progress` step on a v2+ tracker has `last_heartbeat_at`, `heartbeat_interval_seconds`, or `expected_completion_at` missing or of the wrong type — the step is unmonitorable | `verify_step_liveness.py` |
| `ST-E010` | A `paused_waiting` step's `pause_count` exceeds `max_pause_count` (default 12) — the wait is not converging and no one has looked at it | `verify_step_liveness.py` |

### Warnings

| Code | Description | Enforced by |
| --- | --- | --- |
| `ST-W001` | A completed/failed/skipped step has `log_file` set to `null` | `verify_logs.py`, as `LG-W006` |
| `ST-W002` | `log_file` path does not point to an existing file | **not enforced** |
| `ST-W003` | `started_at` is `null` for a non-pending step | **not enforced** |
| `ST-W004` | `completed_at` is `null` for a completed/failed/skipped step | **not enforced** |
| `ST-W005` | An `in_progress` step has a stale heartbeat AND no live VM (ghosted step, no billing risk) | `verify_step_liveness.py` |
| `ST-W006` | An `in_progress` step is heart-beating but elapsed wall-clock exceeds `expected_completion_at` by the configured slow factor (default 2×) | `verify_step_liveness.py` |
| `ST-W007` | A completed/failed/skipped step has `actual_duration_seconds` set to `null` | **not enforced** |
| `ST-W008` | The task has a live VM whose `machine_log.json` entry does not record `watchdog_active: true` — an unprotected machine, where a missed wakeup bills until a human notices | `verify_step_liveness.py` |
| `ST-W009` | A `paused_waiting` step has no `liveness_probe` — a job that died mid-wait is indistinguishable from one still running, so the step re-pauses forever | `verify_step_liveness.py` |

"Stale heartbeat" is defined as `(now - last_heartbeat_at) > heartbeat_interval_seconds * 3`. The
factor is configurable via `verify_step_liveness.py --stale-multiplier`.

### Live VM Detection

`ST-E007` and `ST-W005` differ only in whether the task still has a **live VM**. A machine recorded
in a `machine_log.json` entry under `logs/steps/*setup-machines*/` counts as live when both hold:

* `instance_id` is a non-empty string — the instance was actually created, and
* `destroyed_at` is `null` or absent — nothing has recorded its destruction.

This is the same "still alive" signal `RM-E001` uses in
`arf/specifications/remote_machines_specification.md`, and it relies only on fields the machine-log
schema guarantees. Do **not** detect liveness from a provider status field such as Vast.ai's
`actual_status`: those describe a live provider API response, are never persisted into
`machine_log.json`, and a detector that reads them from the log silently classifies every machine as
destroyed — downgrading the `ST-E007` idle-billing error to an `ST-W005` warning and letting the
verificator exit `0` while a GPU bills.

### Backward Compatibility

Trackers without `spec_version` (i.e., v1 files written before v2) are treated as read-only by the
liveness verificator: it silently skips steps that lack the liveness fields, emitting neither errors
nor warnings.

Trackers that declare a `spec_version` are held to the full contract: an `in_progress` step whose
`last_heartbeat_at`, `heartbeat_interval_seconds`, or `expected_completion_at` is missing or of the
wrong type is flagged `ST-E009`. Such a step can never be classified as ghosted or slow, so silently
skipping it would hide exactly the runaway it exists to catch.
