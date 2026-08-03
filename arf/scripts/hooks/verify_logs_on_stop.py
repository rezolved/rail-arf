"""Stop hook: refuse to end a turn on an unattended step, then verify logs.

Called by Claude Code whenever the agent attempts to stop. Two jobs, in order:

1. **Block the stop** (exit 2, message on stderr) when a live task still carries an
   ``in_progress`` step. Nothing polls a task on its own: a turn that ends with a step
   still marked ``in_progress`` leaves the work with no component scheduled to return to
   it. That is the failure behind Lesson 8 in ``LESSONS.md`` — a step-executor walked away
   from a running training job and an H100 sat idle for ~20 hours.

   ``execute-task`` has forbidden this in prose since v26 and it happened anyway. This
   hook is the enforcement point, because it is the only component guaranteed to run on
   every stop.

   The exits the agent may legally take are both cheap:
   * drive the step to a terminal state (``poststep``), or
   * hand it over — ``heartbeat pause`` records ``resume_after`` and a ``liveness_probe``,
     moving the step to ``paused_waiting`` — and register a wakeup that will drive it.

2. **Verify logs** (advisory, never blocking) when on a task branch, unchanged.

Why ``in_progress`` and not a stale heartbeat: at the moment a turn ends the heartbeat is
still fresh, so a staleness check passes and the stop is allowed — and there is no later
turn whose stop could catch it. The condition has to be "you are leaving work behind",
which is true immediately.

The hook cannot see whether a wakeup was actually registered — schedules live in session
memory, not on disk. It closes the silent exit, not every exit.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
TASKS_DIR: Path = REPO_ROOT / "tasks"
VERIFY_LOGS_SCRIPT: Path = REPO_ROOT / "arf" / "scripts" / "verificators" / "verify_logs.py"
TASK_BRANCH_PREFIX: str = "task/"

STEP_TRACKER_FILENAME: str = "step_tracker.json"
TASK_JSON_FILENAME: str = "task.json"
STEPS_FIELD: str = "steps"
STEP_FIELD: str = "step"
NAME_FIELD: str = "name"
STATUS_FIELD: str = "status"
STOP_HOOK_ACTIVE_FIELD: str = "stop_hook_active"

STEP_STATUS_IN_PROGRESS: str = "in_progress"
TASK_STATUS_IN_PROGRESS: str = "in_progress"
BLOCK_EXIT_CODE: int = 2


@dataclass(frozen=True, slots=True)
class UnattendedStep:
    task_id: str
    step_number: int | None
    step_name: str | None


def _load_json_object(*, path: Path) -> dict[str, object] | None:
    """Read a JSON object, or ``None`` when unreadable.

    A malformed tracker must not block every stop in the repository, so every failure
    here degrades to "nothing to report" rather than raising.
    """
    try:
        raw: str = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _read_stop_hook_active() -> bool:
    """Report whether this stop was already blocked once in the current turn.

    Claude Code sets ``stop_hook_active`` on the payload when the agent is continuing
    because a Stop hook blocked it. Without honouring it the turn could never end.
    """
    try:
        raw: str = sys.stdin.read()
    except (OSError, ValueError):
        return False
    if len(raw.strip()) == 0:
        return False
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get(STOP_HOOK_ACTIVE_FIELD) is True


def _task_is_live(*, task_dir: Path) -> bool:
    """Report whether the task itself is still running.

    Gates the block on live tasks only. A completed task holding a stale ``in_progress``
    step would otherwise brick every stop in the repository forever, and such stale data
    exists (see the t0008 machine-log entry in the remote-run reliability handoff).
    """
    task_json: dict[str, object] | None = _load_json_object(
        path=task_dir / TASK_JSON_FILENAME,
    )
    if task_json is None:
        return False
    return task_json.get(STATUS_FIELD) == TASK_STATUS_IN_PROGRESS


def find_unattended_steps(*, tasks_dir: Path) -> list[UnattendedStep]:
    if not tasks_dir.is_dir():
        return []

    unattended: list[UnattendedStep] = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        if not _task_is_live(task_dir=task_dir):
            continue
        tracker: dict[str, object] | None = _load_json_object(
            path=task_dir / STEP_TRACKER_FILENAME,
        )
        if tracker is None:
            continue
        steps: object = tracker.get(STEPS_FIELD)
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get(STATUS_FIELD) != STEP_STATUS_IN_PROGRESS:
                continue
            step_number: object = step.get(STEP_FIELD)
            step_name: object = step.get(NAME_FIELD)
            unattended.append(
                UnattendedStep(
                    task_id=task_dir.name,
                    step_number=step_number if isinstance(step_number, int) else None,
                    step_name=step_name if isinstance(step_name, str) else None,
                ),
            )
    return unattended


def format_block_message(*, steps: list[UnattendedStep]) -> str:
    lines: list[str] = ["Refusing to end the turn: work is still marked in_progress.", ""]
    for step in steps:
        number: str = "?" if step.step_number is None else str(step.step_number)
        name: str = "unnamed" if step.step_name is None else step.step_name
        lines.append(f"  * {step.task_id} step {number} ({name})")
    lines.extend(
        [
            "",
            "Nothing polls a task on its own. Ending the turn here leaves this step with",
            "no component scheduled to return to it — the failure in LESSONS.md Lesson 8.",
            "",
            "This check is repo-wide. If a step above is not the one this session has been",
            "driving, another session may be running it right now: do not touch it, say so,",
            "and stop again — the repeat stop is allowed.",
            "",
            "For the step this session owns, take one of the two exits, then stop again:",
            "",
            "  1. Drive the step to a terminal state (poststep marks it completed).",
            "",
            "  2. Hand it over, if it is waiting on something remote:",
            "       uv run python -m arf.scripts.utils.heartbeat pause <task_id> <step> \\",
            "           --resume-sentinel '<what you are waiting for>' \\",
            "           --resume-after '<ISO 8601 UTC>' --watchdog-active \\",
            "           --liveness-probe 'ssh <host> tmux has-session -t <session>'",
            "     Then register the wakeup that will actually drive the resume — a",
            "     ScheduleWakeup, or a /loop tick. A pause with no wakeup is the same bug.",
        ],
    )
    return "\n".join(lines)


def _get_current_branch() -> str | None:
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except OSError:
        return None


def _run_logs_advisory() -> None:
    branch: str | None = _get_current_branch()
    if branch is None or not branch.startswith(TASK_BRANCH_PREFIX):
        return

    task_id: str = branch[len(TASK_BRANCH_PREFIX) :]
    logs_path: Path = TASKS_DIR / task_id / "logs"
    if not logs_path.is_dir():
        return

    result: subprocess.CompletedProcess[str] = subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(VERIFY_LOGS_SCRIPT),
            task_id,
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    if len(result.stdout) > 0:
        print(result.stdout)
    if len(result.stderr) > 0:
        print(result.stderr, file=sys.stderr)


def main() -> None:
    if not _read_stop_hook_active():
        unattended: list[UnattendedStep] = find_unattended_steps(tasks_dir=TASKS_DIR)
        if len(unattended) > 0:
            sys.stderr.write(format_block_message(steps=unattended) + "\n")
            sys.exit(BLOCK_EXIT_CODE)

    _run_logs_advisory()
    sys.exit(0)


if __name__ == "__main__":
    main()
