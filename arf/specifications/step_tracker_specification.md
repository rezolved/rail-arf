# Step Tracker Specification

**Version**: 3

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
| `spec_version` | string | yes | Specification version (e.g., `"2"`) |
| `task_id` | string | yes | Must match the task folder name |
| `steps` | list[Step] | yes | Ordered list of task steps |

The `spec_version` field is required starting at v2. v1 trackers without it are treated as
backward-compatible and silently skipped by the liveness verificator.

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
| `actual_duration_seconds` | int \| null | yes when completed | Wall-clock duration in seconds, written when the step transitions to `completed`, `failed`, or `skipped` |
| `resume_sentinel` | string \| null | yes when paused_waiting | What the step is waiting on and how to re-check it on resume (e.g., benchmark output path on the VM) |
| `paused_at` | string \| null | yes when paused_waiting | ISO 8601 UTC timestamp the step entered `paused_waiting` |
| `resume_after` | string \| null | yes when paused_waiting | ISO 8601 UTC earliest time the orchestrator should attempt resume |
| `watchdog_active` | bool \| null | yes when paused_waiting | `true` iff the VM carries an idle dead-man's-switch watchdog; pausing requires this |

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

### Paused steps and resume-from-files

`paused_waiting` lets a long GPU wait release its session instead of holding a warm context idle for
hours (the token cost the watchdog was built to remove). The owner calls
`arf.scripts.utils.heartbeat.pause_step` (or `heartbeat pause` on the CLI), which records
`resume_sentinel`, `paused_at`, `resume_after`, and `watchdog_active`, clears `current_owner`, and
returns. A later `execute-task` wakeup re-reads the tracker, and once `now >= resume_after`
re-dispatches the step's skill to re-check the sentinel and either complete or re-pause.

Pausing is safe **only** when the VM carries an idle dead-man's-switch watchdog — otherwise a missed
resume leaves the box billing, which is the banned fire-and-forget pattern (`LESSONS.md` Lesson 9).
`verify_step_liveness` enforces this: a `paused_waiting` step with `watchdog_active != true` is
flagged `ST-E008` (error). A `paused_waiting` step with `watchdog_active == true` is a deliberate,
safe wait and is never treated as ghosted, regardless of heartbeat age.

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

* * *

## Example

```json
{
  "spec_version": "2",
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

| Code | Description |
| --- | --- |
| `ST-E001` | `step_tracker.json` does not exist or is not valid JSON |
| `ST-E002` | `task_id` does not match the task folder name |
| `ST-E003` | `steps` is missing or not a list |
| `ST-E004` | A step is missing required fields (`step`, `name`, `description`, `status`) |
| `ST-E005` | Step numbers are not sequential starting from 1 |
| `ST-E006` | `status` is not one of the allowed values |
| `ST-E007` | An `in_progress` step has a stale heartbeat AND a live VM is provisioned for the task (idle billing risk) |
| `ST-E008` | An `in_progress` step has `last_heartbeat_at`, `heartbeat_interval_seconds`, or `expected_completion_at` missing — required at v2 for any `in_progress` step |

### Warnings

| Code | Description |
| --- | --- |
| `ST-W001` | A completed/failed/skipped step has `log_file` set to `null` |
| `ST-W002` | `log_file` path does not point to an existing file |
| `ST-W003` | `started_at` is `null` for a non-pending step |
| `ST-W004` | `completed_at` is `null` for a completed/failed/skipped step |
| `ST-W005` | An `in_progress` step has a stale heartbeat AND no live VM (ghosted step, no billing risk) |
| `ST-W006` | An `in_progress` step is heart-beating but elapsed wall-clock exceeds `expected_completion_at` by the configured slow factor (default 2×) |
| `ST-W007` | A completed/failed/skipped step has `actual_duration_seconds` set to `null` |

"Stale heartbeat" is defined as `(now - last_heartbeat_at) > heartbeat_interval_seconds * 3`. The
factor is configurable via `verify_step_liveness.py --stale-multiplier`.

### Backward Compatibility

Trackers without `spec_version` (i.e., v1 files written before this version) are treated as
read-only by the liveness verificator: it silently skips steps that lack the four liveness fields,
emitting neither errors nor warnings. New tasks created under v2 must populate the liveness fields
for any `in_progress` step.
