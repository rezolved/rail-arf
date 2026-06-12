---
name: "diagnose-stuck-step"
description: >-
  Read-only, time-boxed diagnostic for an in_progress task step flagged by
  verify_step_liveness as ghosted (ST-E007 / ST-W005) or pathologically slow
  (ST-W006). Produces a structured JSON report and exits without mutating
  remote machines or the task state. Use whenever the orchestrator detects
  a step that has stopped making progress.
---
# Diagnose Stuck Step

**Version**: 1

## Goal

Within a five-minute time budget, determine why a flagged in-progress step is no longer making
progress, and produce a structured JSON report at
`tasks/<task_id>/logs/diagnostics/<timestamp>_<step_number>.json` so the orchestrator can decide
between resuming inline, tearing down the VM and restarting, or escalating to a human.

The skill is read-only: no mutations, no SSH writes, no VM teardown authority. It exists only to
inform the orchestrator's recovery decision.

## Inputs

* `$TASK_ID` — the task folder name (e.g., `t0029_dev_vs_vm_rag_agentic_pc_ablation`)
* `$STEP_NUMBER` — the step number reported as stuck by `verify_step_liveness`
* `$MODE` (optional, default `auto`) — `ghosted` (forces the stale-heartbeat code path), `slow`
  (forces the pathologically-slow code path), or `auto` (inferred from the step's liveness fields).

## Context

Read before starting:

* `arf/specifications/step_tracker_specification.md` — v2 schema with liveness fields.
* `arf/specifications/remote_machines_specification.md` — `machine_log.json` shape (used to locate
  the VM and its SSH coordinates).
* `arf/scripts/utils/ssh_health_probe.py` — the shared health-probe primitive used here and by
  `setup-remote-machine` Phase 4 (Lesson 2 smoke gate).
* `LESSONS.md` Lesson 9 (or its successor) — the incident that motivated this skill.
* `tasks/$TASK_ID/step_tracker.json` — to read the flagged step's liveness fields.
* The relevant step log folder under `tasks/$TASK_ID/logs/steps/` — its name is derived from
  `step_tracker.json` `log_file` (when set) or by glob:
  `tasks/$TASK_ID/logs/steps/<step_number_padded>_*/`.

## Critical Rules

1. **Read-only**. NEVER run remote commands that mutate state. The allowed SSH primitives are
   exactly those in `arf.scripts.utils.ssh_health_probe.probe`: `tmux list-sessions`, two
   `nvidia-smi --query-gpu=…` invocations, an optional `tail -n 200 <log>`, plus optional HTTP GET
   to `/health` and POST to `/v1/completions`. Adding any other remote command is forbidden.
2. **Time-boxed**. The diagnostic must complete within five wall-clock minutes. Each probe carries a
   30-second timeout. If a probe hangs, record `engine_state = "unknown"` and proceed.
3. **No teardown authority**. NEVER call `az ml compute stop`, `azure_ml_vm teardown`,
   `vastai destroy`, or any equivalent. The orchestrator owns the teardown decision based on this
   skill's report.
4. **Heartbeat discipline applies to this skill too**. Before doing any probe, call
   `arf.scripts.utils.heartbeat.start_step` for the step that owns this diagnostic invocation
   (typically a sub-step under the parent task) with `heartbeat_interval_seconds=60` and
   `expected_completion_at` five minutes in the future. If a probe runs long, refresh the heartbeat
   between probes.
5. **Output exactly one report file**. The skill always writes
   `tasks/$TASK_ID/logs/diagnostics/<timestamp>_<step_number>.json`. On any internal failure, the
   report still gets written with `engine_state = "unknown"` and the failure mode captured in
   `likely_cause`. Never exit without writing the report.
6. **No project-specific assumptions**. The skill is framework-generic. It must work for any project
   that follows the ARF step_tracker + machine_log conventions.

## Steps

### Step 1: Read the flagged step

1. Load `tasks/$TASK_ID/step_tracker.json` and find the step whose `step` field equals
   `$STEP_NUMBER`. If the step is missing, write a report with `engine_state = "unknown"`,
   `likely_cause = "step <N> not present in step_tracker.json"`,
   `recommended_action = "intervention_required"`, and exit.
2. Capture from the step: `status`, `started_at`, `last_heartbeat_at`, `expected_completion_at`,
   `heartbeat_interval_seconds`, `current_owner`, `log_file`.
3. If `status != "in_progress"`, the step is not in fact stuck. Write a report with
   `recommended_action = "intervention_required"` and a `likely_cause` noting the unexpected status.
   The orchestrator should not have routed here.

### Step 2: Resolve VM coordinates (if any)

1. Glob `tasks/$TASK_ID/logs/steps/*setup-machines*/machine_log.json`. If no file exists, the task
   has no VM — proceed to Step 3 with `vm_present = False`.
2. Otherwise, parse the file (a JSON array of machine objects). The first entry with
   `actual_status == "running"` is the active VM. Capture `ssh_host`, `ssh_port`, `ssh_user`
   (default to `azureuser` when not present), and any engine URL recorded in `machine_log.json`
   (look for a top-level `engine_url`, or fall back to constructing `http://<ssh_host>:8000` only
   when the engine has been previously confirmed up — otherwise leave it `None`).
3. If no `running` entry exists, `vm_present = False`.

### Step 3: Run the read-only probe (VM case)

Only when `vm_present == True`:

1. Call `arf.scripts.utils.ssh_health_probe.probe(...)` with the VM coordinates. Always pass the
   30-second `timeout_seconds` default. Pass `log_path` set to a path under `/home/<ssh_user>/` only
   when one is recorded in `machine_log.json` `output_log_path`; never guess.
2. Capture the returned `SshHealthReport`.
3. Map the report onto an `engine_state`:
   * `ssh_reachable == False` → `dead`.
   * `engine_health_ok == True` AND `engine_completion_ok == True` AND
     `gpu_utilization_percent >= 1` → `alive`.
   * `engine_health_ok == True` AND `engine_completion_ok == False` → `degraded`.
   * `engine_health_ok == True` AND `gpu_utilization_percent == 0` → `degraded` (engine up but no
     work running — the typical idle-billing fingerprint).
   * `engine_health_ok == False` → `degraded`.
   * Otherwise → `unknown`.

### Step 4: Run the non-VM diagnostic (no-VM case)

When `vm_present == False` (this is the path that handles the "12h of waits/polls/post-processing"
class of failure):

1. Run `tmux list-sessions` locally — there should be none for ARF tasks; record any that are
   present in `likely_cause`.
2. Compute the most recently modified file under `tasks/$TASK_ID/logs/steps/` (use Python
   `pathlib.Path.stat().st_mtime`, do not spawn `find`). Capture its path and mtime — if mtime is
   older than the heartbeat-interval × stale-multiplier, that's a strong signal that the step really
   stopped producing log output.
3. Read the last 200 lines of the most recently modified `*.log`, `step_log.md`, or `stdout.log`
   file under the step's log folder. Truncate to ~12 kB to keep the report bounded.
4. List the current process tree filtered to `pytest`, `ruff`, `mypy`, `flowmark`, `python`, `gh`,
   and `git` invocations (cross-platform via Python `psutil` if available; otherwise `ps -ef` parsed
   in Python). Truncate to the top 30 entries.
5. Map onto an `engine_state`:
   * Recent file mtime fresh AND a relevant Python process is present → `alive`.
   * Recent file mtime stale AND no relevant Python process → `dead`.
   * Recent file mtime fresh AND no relevant process → `degraded` (probably waiting on a subprocess
     that was spawned and detached).
   * Otherwise → `unknown`.

### Step 5: Compute `last_observed_progress`

`last_observed_progress` is the most recent of: the step's `last_heartbeat_at`, the most recent file
mtime under the step's log folder, and (when VM present) the tmux session start time if it can be
parsed cleanly. Record it as an ISO 8601 UTC timestamp, plus a short string describing what produced
it (e.g., `"step_log.md mtime"`).

### Step 6: Pick `recommended_action`

Apply this decision table:

| `engine_state` | VM present | Recommendation |
| --- | --- | --- |
| `alive` | yes / no | `resume_inline` |
| `degraded` | yes | `teardown_and_restart` if engine_health_ok is False; otherwise `resume_inline` and flag in `likely_cause` |
| `degraded` | no | `resume_inline` (the orchestrator owns the recovery) |
| `dead` | yes | `teardown_and_restart` |
| `dead` | no | `intervention_required` |
| `unknown` | yes / no | `intervention_required` |

### Step 7: Write the report

Write JSON to `tasks/$TASK_ID/logs/diagnostics/<timestamp>_<step_number>.json` where `<timestamp>`
is `YYYYMMDDTHHMMSSZ`. The directory must exist (create with `mkdir -p` first). Schema:

```json
{
  "spec_version": "1",
  "task_id": "t0029_dev_vs_vm_rag_agentic_pc_ablation",
  "step_number": 9,
  "produced_at": "2026-05-20T06:17:00Z",
  "mode": "ghosted",
  "engine_state": "degraded",
  "vm_present": true,
  "last_observed_progress": {
    "timestamp": "2026-05-19T20:59:23Z",
    "source": "tmux engine session start"
  },
  "ssh_report": {
    "ssh_reachable": true,
    "tmux_sessions": ["vllm"],
    "gpu_utilization_percent": 0,
    "gpu_memory_used_mb": 78340,
    "engine_health_ok": true,
    "engine_completion_ok": true,
    "recent_log_tail": "…"
  },
  "non_vm_report": null,
  "likely_cause": "Engine up, model loaded into GPU memory, but GPU utilization 0% for the last 9 hours — no client driving the engine. Looks like the implementation subagent ghosted after warm-up.",
  "recoverable": "yes",
  "recommended_action": "resume_inline"
}
```

Field semantics:

* `mode` — `ghosted`, `slow`, or `auto`.
* `engine_state` — `alive`, `degraded`, `dead`, `unknown`.
* `vm_present` — boolean.
* `ssh_report` — present iff `vm_present`. Mirrors `SshHealthReport` from
  `arf.scripts.utils.ssh_health_probe`.
* `non_vm_report` — present iff `vm_present == False`. Includes `recent_log_path`,
  `recent_log_mtime_utc`, `recent_log_tail`, `relevant_processes` (list of strings),
  `local_tmux_sessions` (list of strings).
* `likely_cause` — free-text, one to three sentences, written by the agent based on the structured
  observations above. Plain English; this is what the orchestrator's recovery decision is grounded
  in.
* `recoverable` — `yes` / `no` / `unclear`.
* `recommended_action` — `resume_inline` / `teardown_and_restart` / `intervention_required`.

### Step 8: Print the report path

Print exactly the absolute path to the report file as the last line of stdout. The orchestrator
parses this last line.

## Output Format

Exactly one JSON file matching the schema in Step 7, plus the report path printed as the final
stdout line.

## Done When

* A JSON report has been written at
  `tasks/$TASK_ID/logs/diagnostics/<timestamp>_<step_number>.json`.
* `recommended_action` is one of the three allowed values.
* The skill exited within five wall-clock minutes.
* No remote mutation, teardown, or write was performed.
* Heartbeat discipline was applied to this skill's own work.

## Forbidden

* NEVER mutate a remote machine. The skill is observation-only.
* NEVER tear down a VM. That decision belongs to the orchestrator after reading the report.
* NEVER skip writing the report. If everything fails, write a report with `engine_state="unknown"`
  and a clear `likely_cause`.
* NEVER add SSH commands beyond those exposed by `arf.scripts.utils.ssh_health_probe.probe`.
* NEVER consult the parent task's `task.json` to make project-specific recommendations. This skill
  is framework-generic.
