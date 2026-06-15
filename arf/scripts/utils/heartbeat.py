"""Step-tracker heartbeat helper.

Owners of an ``in_progress`` step call this module to maintain the liveness fields defined in
``arf/specifications/step_tracker_specification.md``:

* ``start_step`` — transitions a step from ``pending`` to ``in_progress`` and initializes the
  liveness fields (``current_owner``, ``last_heartbeat_at``, ``heartbeat_interval_seconds``,
  ``expected_completion_at``).
* ``write_heartbeat`` — refreshes ``last_heartbeat_at`` and ``current_owner`` on a step that is
  already ``in_progress``.
* ``complete_step`` — transitions the step to ``completed`` and computes
  ``actual_duration_seconds`` from ``started_at`` to ``completed_at``.

A subagent driving a long-running step must call ``write_heartbeat`` at least once per
``heartbeat_interval_seconds`` window or the liveness verificator will eventually flag the step
as ghosted.

CLI form:

```text
python -m arf.scripts.utils.heartbeat write <task_id> <step_number> <owner>
python -m arf.scripts.utils.heartbeat start <task_id> <step_number> <owner> \
    --interval-seconds 300 --expected-completion-at 2026-05-20T12:00:00Z
python -m arf.scripts.utils.heartbeat complete <task_id> <step_number>
```
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from arf.scripts.verificators.common import paths

STATUS_FIELD: str = "status"
STATUS_PENDING: str = "pending"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_COMPLETED: str = "completed"
STATUS_PAUSED_WAITING: str = "paused_waiting"

RESUME_SENTINEL_FIELD: str = "resume_sentinel"
PAUSED_AT_FIELD: str = "paused_at"
RESUME_AFTER_FIELD: str = "resume_after"
WATCHDOG_ACTIVE_FIELD: str = "watchdog_active"

STEPS_FIELD: str = "steps"
STEP_FIELD: str = "step"
STARTED_AT_FIELD: str = "started_at"
COMPLETED_AT_FIELD: str = "completed_at"
LAST_HEARTBEAT_AT_FIELD: str = "last_heartbeat_at"
CURRENT_OWNER_FIELD: str = "current_owner"
HEARTBEAT_INTERVAL_FIELD: str = "heartbeat_interval_seconds"
EXPECTED_COMPLETION_FIELD: str = "expected_completion_at"
ACTUAL_DURATION_FIELD: str = "actual_duration_seconds"


@dataclass(frozen=True, slots=True)
class StepLocation:
    tracker_path: Path
    tracker: dict[str, object]
    step: dict[str, object]


def _now_iso8601_utc() -> str:
    now: datetime = datetime.now(tz=UTC).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso8601(*, value: str) -> datetime:
    normalized: str = value.replace("Z", "+00:00")
    parsed: datetime = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _load_tracker(*, task_id: str) -> tuple[Path, dict[str, object]]:
    tracker_path: Path = paths.step_tracker_path(task_id=task_id)
    if not tracker_path.exists():
        raise FileNotFoundError(
            f"step_tracker.json not found for task {task_id} at {tracker_path}",
        )
    raw: str = tracker_path.read_text(encoding="utf-8")
    data: object = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            f"step_tracker.json at {tracker_path} is not a JSON object",
        )
    return tracker_path, data


def _find_step(
    *,
    tracker: dict[str, object],
    step_number: int,
) -> dict[str, object]:
    steps_obj: object = tracker.get(STEPS_FIELD)
    if not isinstance(steps_obj, list):
        raise ValueError(
            f"step_tracker.json is missing or has invalid '{STEPS_FIELD}' list",
        )
    for entry in steps_obj:
        if not isinstance(entry, dict):
            continue
        if entry.get(STEP_FIELD) == step_number:
            return entry
    raise ValueError(
        f"step {step_number} not found in step_tracker.json",
    )


def _locate(*, task_id: str, step_number: int) -> StepLocation:
    tracker_path, tracker = _load_tracker(task_id=task_id)
    step: dict[str, object] = _find_step(tracker=tracker, step_number=step_number)
    return StepLocation(tracker_path=tracker_path, tracker=tracker, step=step)


def _write_tracker(*, tracker_path: Path, tracker: dict[str, object]) -> None:
    rendered: str = json.dumps(tracker, indent=2) + "\n"
    tracker_path.write_text(rendered, encoding="utf-8")


def start_step(
    *,
    task_id: str,
    step_number: int,
    current_owner: str,
    heartbeat_interval_seconds: int,
    expected_completion_at: str,
) -> None:
    """Transition a step from ``pending`` to ``in_progress`` and initialize liveness fields."""

    location: StepLocation = _locate(task_id=task_id, step_number=step_number)
    now: str = _now_iso8601_utc()

    location.step[STATUS_FIELD] = STATUS_IN_PROGRESS
    location.step[STARTED_AT_FIELD] = now
    location.step[CURRENT_OWNER_FIELD] = current_owner
    location.step[LAST_HEARTBEAT_AT_FIELD] = now
    location.step[HEARTBEAT_INTERVAL_FIELD] = heartbeat_interval_seconds
    location.step[EXPECTED_COMPLETION_FIELD] = expected_completion_at

    _write_tracker(tracker_path=location.tracker_path, tracker=location.tracker)


def write_heartbeat(
    *,
    task_id: str,
    step_number: int,
    current_owner: str,
) -> None:
    """Refresh ``last_heartbeat_at`` and ``current_owner`` on a step that is already in progress."""

    location: StepLocation = _locate(task_id=task_id, step_number=step_number)
    location.step[LAST_HEARTBEAT_AT_FIELD] = _now_iso8601_utc()
    location.step[CURRENT_OWNER_FIELD] = current_owner

    _write_tracker(tracker_path=location.tracker_path, tracker=location.tracker)


def complete_step(*, task_id: str, step_number: int) -> None:
    """Transition a step to ``completed``, compute duration, and clear ``current_owner``."""

    location: StepLocation = _locate(task_id=task_id, step_number=step_number)
    now: str = _now_iso8601_utc()

    started_at_obj: object = location.step.get(STARTED_AT_FIELD)
    duration_seconds: int = 0
    if isinstance(started_at_obj, str):
        started_dt: datetime = _parse_iso8601(value=started_at_obj)
        completed_dt: datetime = _parse_iso8601(value=now)
        duration_seconds = max(0, int((completed_dt - started_dt).total_seconds()))

    location.step[STATUS_FIELD] = STATUS_COMPLETED
    location.step[COMPLETED_AT_FIELD] = now
    location.step[ACTUAL_DURATION_FIELD] = duration_seconds
    location.step[CURRENT_OWNER_FIELD] = None

    _write_tracker(tracker_path=location.tracker_path, tracker=location.tracker)


def pause_step(
    *,
    task_id: str,
    step_number: int,
    resume_sentinel: str,
    resume_after: str,
    watchdog_active: bool,
) -> None:
    """Transition an ``in_progress`` step to ``paused_waiting`` for a long external wait.

    The owner records what it is waiting on (``resume_sentinel``), the earliest time the
    orchestrator should attempt resume (``resume_after``), and whether the VM carries an idle
    dead-man's-switch watchdog (``watchdog_active``). Pausing is only safe when ``watchdog_active``
    is ``True`` — otherwise a missed resume would leave the box billing (the banned fire-and-forget
    pattern, ``LESSONS.md`` Lesson 8). ``current_owner`` is cleared because no one is driving the
    step while paused; ``verify_step_liveness`` treats ``paused_waiting`` as a non-ghost state.
    """

    assert watchdog_active, (
        "pause_step requires an active idle watchdog on the VM; pausing without one is the banned "
        "fire-and-forget pattern (LESSONS Lesson 8)."
    )
    location: StepLocation = _locate(task_id=task_id, step_number=step_number)

    location.step[STATUS_FIELD] = STATUS_PAUSED_WAITING
    location.step[CURRENT_OWNER_FIELD] = None
    location.step[RESUME_SENTINEL_FIELD] = resume_sentinel
    location.step[PAUSED_AT_FIELD] = _now_iso8601_utc()
    location.step[RESUME_AFTER_FIELD] = resume_after
    location.step[WATCHDOG_ACTIVE_FIELD] = watchdog_active

    _write_tracker(tracker_path=location.tracker_path, tracker=location.tracker)


def _build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="arf.scripts.utils.heartbeat",
        description="Update step_tracker.json liveness fields for a task step.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    write_p = subparsers.add_parser("write", help="Refresh last_heartbeat_at.")
    write_p.add_argument("task_id", type=str)
    write_p.add_argument("step_number", type=int)
    write_p.add_argument("owner", type=str)

    start_p = subparsers.add_parser("start", help="Mark the step in_progress.")
    start_p.add_argument("task_id", type=str)
    start_p.add_argument("step_number", type=int)
    start_p.add_argument("owner", type=str)
    start_p.add_argument(
        "--interval-seconds",
        type=int,
        required=True,
        help="Heartbeat cadence in seconds.",
    )
    start_p.add_argument(
        "--expected-completion-at",
        type=str,
        required=True,
        help="ISO 8601 UTC expected completion timestamp (e.g. 2026-05-20T12:00:00Z).",
    )

    complete_p = subparsers.add_parser("complete", help="Mark the step completed.")
    complete_p.add_argument("task_id", type=str)
    complete_p.add_argument("step_number", type=int)

    pause_p = subparsers.add_parser(
        "pause",
        help="Pause an in_progress step on a long external wait (requires an active VM watchdog).",
    )
    pause_p.add_argument("task_id", type=str)
    pause_p.add_argument("step_number", type=int)
    pause_p.add_argument(
        "--resume-sentinel",
        type=str,
        required=True,
        help="What the step is waiting on and how to re-check it on resume.",
    )
    pause_p.add_argument(
        "--resume-after",
        type=str,
        required=True,
        help="ISO 8601 UTC earliest time to attempt resume (e.g. 2026-06-12T15:30:00Z).",
    )
    pause_p.add_argument(
        "--watchdog-active",
        action="store_true",
        help="Assert the VM carries an idle dead-man's-switch watchdog. Required to pause safely.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = _build_parser()
    args: argparse.Namespace = parser.parse_args(argv)

    if args.action == "write":
        write_heartbeat(
            task_id=args.task_id,
            step_number=args.step_number,
            current_owner=args.owner,
        )
    elif args.action == "start":
        start_step(
            task_id=args.task_id,
            step_number=args.step_number,
            current_owner=args.owner,
            heartbeat_interval_seconds=args.interval_seconds,
            expected_completion_at=args.expected_completion_at,
        )
    elif args.action == "complete":
        complete_step(
            task_id=args.task_id,
            step_number=args.step_number,
        )
    elif args.action == "pause":
        if not args.watchdog_active:
            parser.error(
                "pause requires --watchdog-active: pausing a step without an active VM "
                "watchdog is the banned fire-and-forget pattern (LESSONS Lesson 8)."
            )
        pause_step(
            task_id=args.task_id,
            step_number=args.step_number,
            resume_sentinel=args.resume_sentinel,
            resume_after=args.resume_after,
            watchdog_active=args.watchdog_active,
        )
    else:
        parser.error(f"unknown action: {args.action}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
