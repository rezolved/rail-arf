# Utilities Reference

Helper scripts for task execution. They live in `arf/scripts/utils/`.

## All Utilities

| Script | Purpose |
| --- | --- |
| [`prestep.py`](../../scripts/utils/prestep.py) | Pre-step validation: check preconditions, mark step `in_progress`, create log folder |
| [`poststep.py`](../../scripts/utils/poststep.py) | Post-step verification: validate outputs, mark step `completed`, auto-commit `step_tracker.json` |
| [`run_with_logs.py`](../../scripts/utils/run_with_logs.py) | Wrap CLI commands; capture stdout/stderr; write JSON metadata to `logs/commands/` |
| [`init_task_folders.py`](../../scripts/utils/init_task_folders.py) | Create mandatory task folder structure from `task.json` |
| [`worktree.py`](../../scripts/utils/worktree.py) | Create and manage git worktrees for task isolation |
| [`doi_to_slug.py`](../../scripts/utils/doi_to_slug.py) | Convert a DOI to a deterministic folder-name slug |
| [`find_similar_papers.py`](../../scripts/utils/find_similar_papers.py) | Find similar papers across the corpus for deduplication |
| [`capture_task_sessions.py`](../../scripts/utils/capture_task_sessions.py) | Capture CLI session transcripts for a task |
| [`skip_step.py`](../../scripts/utils/skip_step.py) | Mark steps as skipped and create their step logs |
| [`heartbeat.py`](../../scripts/utils/heartbeat.py) | Maintain step-tracker liveness fields (`start_step`, `write_heartbeat`, `complete_step`) for any owner of an `in_progress` step |
| [`ssh_health_probe.py`](../../scripts/utils/ssh_health_probe.py) | Shared read-only SSH health probe (`probe()` → `SshHealthReport`) used by setup-remote-machine smoke gate and diagnose-stuck-step |
| [`watchdog_provisioning.py`](../../scripts/utils/watchdog_provisioning.py) | Render the `idle_watchdog.sh` GPU-idle dead-man's switch into a provider startup hook (vast.ai `onstart`, nebius cloud-init) so a stranded VM self-terminates instead of billing |

## CLI Usage

### prestep

```bash
uv run python -m arf.scripts.utils.prestep <task_id> <step_id>
```

### poststep

```bash
uv run python -m arf.scripts.utils.poststep <task_id> <step_id>
```

### run_with_logs

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id <task_id> -- <command...>
```

All CLI tool calls inside a task branch must be wrapped with `run_with_logs`.

### init_task_folders

```bash
uv run python -m arf.scripts.utils.init_task_folders <task_id>
```

### worktree

```bash
uv run python -m arf.scripts.utils.worktree create <task_id>
uv run python -m arf.scripts.utils.worktree remove <task_id>
```

### doi_to_slug

```bash
uv run python -m arf.scripts.utils.doi_to_slug "<doi>"
```

### find_similar_papers

```bash
uv run python -m arf.scripts.utils.find_similar_papers --title "Paper Title" \
    [--authors "Author One" "Author Two"] [--doi "10.1234/example"] [--year 2025] \
    [--threshold 0.7]
```

### capture_task_sessions

```bash
uv run python -m arf.scripts.utils.capture_task_sessions --task-id <task_id>
```

### skip_step

```bash
uv run python -m arf.scripts.utils.skip_step <task_id> <step_id> "<reason>" \
    [<step_id> "<reason>" ...]
```

### heartbeat

```bash
uv run python -m arf.scripts.utils.heartbeat start <task_id> <step_number> <owner> \
    --interval-seconds 300 --expected-completion-at 2026-05-20T12:00:00Z
uv run python -m arf.scripts.utils.heartbeat write <task_id> <step_number> <owner>
uv run python -m arf.scripts.utils.heartbeat complete <task_id> <step_number>
```

The library API exposes `start_step`, `write_heartbeat`, and `complete_step` for use directly inside
long-running Python steps. See `arf/specifications/step_tracker_specification.md` for the v2
liveness fields these helpers write.

### ssh_health_probe

Library-only (no CLI; the only callers are `setup-remote-machine` Phase 4 and the
`/diagnose-stuck-step` skill):

```python
from arf.scripts.utils.ssh_health_probe import probe

report = probe(
    ssh_host="vm.example",
    ssh_port=22,
    ssh_user="azureuser",
    engine_url="http://localhost:8000",
    log_path="/home/azureuser/engine.log",
)
```

Returns a frozen `SshHealthReport` dataclass with `ssh_reachable`, `tmux_sessions`,
`gpu_utilization_percent`, `gpu_memory_used_mb`, `engine_health_ok`, `engine_completion_ok`,
`recent_log_tail`, and `error`. Read-only; never mutates the remote machine.
