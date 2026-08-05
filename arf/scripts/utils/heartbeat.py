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

The step lifecycle scripts (``prestep``, ``poststep``, ``skip_step``) own their own
tracker read/write cycle, so they call the in-memory helpers instead of the functions
above — same field semantics, one implementation:

* ``arm_step_liveness`` — write the liveness fields onto a loaded step and stamp
  ``spec_version`` on its tracker.
* ``finalize_step_liveness`` — clear the owner and record the duration on a terminal step.
* ``expected_completion_from`` — derive ``expected_completion_at`` from a start and a duration.
* ``now_iso8601_utc`` — the one timestamp format every liveness value is written in. Callers
  must use it rather than re-deriving the format, because these helpers parse what it writes.

A subagent driving a long-running step must call ``write_heartbeat`` at least once per
``heartbeat_interval_seconds`` window or the liveness verificator will eventually flag the step
as ghosted.

CLI form:

```text
python -m arf.scripts.utils.heartbeat write <task_id> <step_number> <owner>
python -m arf.scripts.utils.heartbeat start <task_id> <step_number> <owner> \
    --interval-seconds 300 --expected-completion-at 2026-05-20T12:00:00Z
python -m arf.scripts.utils.heartbeat complete <task_id> <step_number>
python -m arf.scripts.utils.heartbeat pause <task_id> <step_number> \
    --resume-sentinel <what to re-check> --resume-after <ISO> --watchdog-active
```
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arf.scripts.verificators.common import paths

STATUS_FIELD: str = "status"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_COMPLETED: str = "completed"
STATUS_PAUSED_WAITING: str = "paused_waiting"

RESUME_SENTINEL_FIELD: str = "resume_sentinel"
PAUSED_AT_FIELD: str = "paused_at"
RESUME_AFTER_FIELD: str = "resume_after"
WATCHDOG_ACTIVE_FIELD: str = "watchdog_active"
LIVENESS_PROBE_FIELD: str = "liveness_probe"
PAUSE_COUNT_FIELD: str = "pause_count"

STEPS_FIELD: str = "steps"
STEP_FIELD: str = "step"
STARTED_AT_FIELD: str = "started_at"
COMPLETED_AT_FIELD: str = "completed_at"
LAST_HEARTBEAT_AT_FIELD: str = "last_heartbeat_at"
CURRENT_OWNER_FIELD: str = "current_owner"
HEARTBEAT_INTERVAL_FIELD: str = "heartbeat_interval_seconds"
EXPECTED_COMPLETION_FIELD: str = "expected_completion_at"
ACTUAL_DURATION_FIELD: str = "actual_duration_seconds"
SPEC_VERSION_FIELD: str = "spec_version"
ISO8601_FORMAT: str = "%Y-%m-%dT%H:%M:%SZ"

# Stamped on a tracker the first time a step is armed under this version, which is
# what lifts it out of the v1 backward-compatibility carve-out in
# verify_step_liveness. Keep in step with step_tracker_specification.md's version.
STEP_TRACKER_SPEC_VERSION: str = "7"

# Safety-net defaults for a caller that does not declare its own cadence. These are
# deliberately looser than the cadence table in step_tracker_specification.md: the
# table is a target for an owner that heartbeats, these are a detection floor for one
# that does not. Stale is interval x 3, so silence is called dead after 90 minutes.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: int = 1800
DEFAULT_EXPECTED_DURATION_SECONDS: int = 3600


@dataclass(frozen=True, slots=True)
class StepLocation:
    tracker_path: Path
    tracker: dict[str, object]
    step: dict[str, object]


def now_iso8601_utc() -> str:
    now: datetime = datetime.now(tz=UTC).replace(microsecond=0)
    return now.strftime(ISO8601_FORMAT)


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


def expected_completion_from(
    *,
    started_at: str,
    expected_duration_seconds: int,
) -> str:
    start: datetime = _parse_iso8601(value=started_at)
    return (start + timedelta(seconds=expected_duration_seconds)).strftime(ISO8601_FORMAT)


def arm_step_liveness(
    *,
    tracker: dict[str, object],
    step: dict[str, object],
    started_at: str,
    current_owner: str,
    heartbeat_interval_seconds: int,
    expected_completion_at: str,
) -> None:
    """Write the liveness fields onto an in-memory step and stamp the tracker.

    Operates on already-loaded structures so a caller that owns the read/write cycle
    (``prestep``) shares one implementation with the standalone CLI, instead of the two
    drifting apart. The caller persists the tracker.
    """

    step[CURRENT_OWNER_FIELD] = current_owner
    step[LAST_HEARTBEAT_AT_FIELD] = started_at
    step[HEARTBEAT_INTERVAL_FIELD] = heartbeat_interval_seconds
    step[EXPECTED_COMPLETION_FIELD] = expected_completion_at
    # setdefault, not assignment: an existing spec_version is the tracker's own and
    # must survive: a mid-flight upgrade never rewrites history.
    tracker.setdefault(SPEC_VERSION_FIELD, STEP_TRACKER_SPEC_VERSION)


def finalize_step_liveness(
    *,
    step: dict[str, object],
    completed_at: str,
) -> None:
    """Clear the owner and record the wall-clock duration on a step reaching a terminal state."""

    step[CURRENT_OWNER_FIELD] = None
    started_at_obj: object = step.get(STARTED_AT_FIELD)
    if not isinstance(started_at_obj, str):
        # No start timestamp means the duration was never measurable — say so with
        # None rather than inventing a zero that reads like a real measurement.
        step[ACTUAL_DURATION_FIELD] = None
        return
    elapsed: float = (
        _parse_iso8601(value=completed_at) - _parse_iso8601(value=started_at_obj)
    ).total_seconds()
    # A negative elapsed means the tracker is corrupt (clock skew, hand edit,
    # completed_at before started_at). Clamping it to 0 would launder that into a
    # plausible "ran instantly"; None says the duration is not measurable.
    step[ACTUAL_DURATION_FIELD] = int(elapsed) if elapsed >= 0 else None


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
    now: str = now_iso8601_utc()

    location.step[STATUS_FIELD] = STATUS_IN_PROGRESS
    location.step[STARTED_AT_FIELD] = now
    arm_step_liveness(
        tracker=location.tracker,
        step=location.step,
        started_at=now,
        current_owner=current_owner,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        expected_completion_at=expected_completion_at,
    )

    _write_tracker(tracker_path=location.tracker_path, tracker=location.tracker)


def write_heartbeat(
    *,
    task_id: str,
    step_number: int,
    current_owner: str,
) -> None:
    """Refresh ``last_heartbeat_at`` and ``current_owner`` on a step that is already in progress."""

    location: StepLocation = _locate(task_id=task_id, step_number=step_number)
    location.step[LAST_HEARTBEAT_AT_FIELD] = now_iso8601_utc()
    location.step[CURRENT_OWNER_FIELD] = current_owner

    _write_tracker(tracker_path=location.tracker_path, tracker=location.tracker)


def complete_step(*, task_id: str, step_number: int) -> None:
    """Transition a step to ``completed``, compute duration, and clear ``current_owner``."""

    location: StepLocation = _locate(task_id=task_id, step_number=step_number)
    now: str = now_iso8601_utc()

    location.step[STATUS_FIELD] = STATUS_COMPLETED
    location.step[COMPLETED_AT_FIELD] = now
    finalize_step_liveness(step=location.step, completed_at=now)

    _write_tracker(tracker_path=location.tracker_path, tracker=location.tracker)


def _next_pause_count(*, step: dict[str, object]) -> int:
    previous: object = step.get(PAUSE_COUNT_FIELD)
    if isinstance(previous, int) and not isinstance(previous, bool):
        return previous + 1
    # A missing or malformed count means this is the first countable pause. Refusing to
    # count would disable ST-E010 on exactly the trackers most likely to be hand-edited.
    return 1


def pause_step(
    *,
    task_id: str,
    step_number: int,
    resume_sentinel: str,
    resume_after: str,
    watchdog_active: bool,
    liveness_probe: str | None,
) -> None:
    """Transition an ``in_progress`` step to ``paused_waiting`` for a long external wait.

    The owner records what it is waiting on (``resume_sentinel``), the earliest time the
    orchestrator should attempt resume (``resume_after``), and whether the VM carries an idle
    dead-man's-switch watchdog (``watchdog_active``). Pausing is only safe when ``watchdog_active``
    is ``True`` — otherwise a missed resume would leave the box billing (the banned fire-and-forget
    pattern, ``LESSONS.md`` Lesson 8). ``current_owner`` is cleared because no one is driving the
    step while paused; ``verify_step_liveness`` treats ``paused_waiting`` as a non-ghost state.

    ``liveness_probe`` is a shell command that exits ``0`` while the remote work is still
    running; ``arf/scripts/utils/resume_check.py`` runs it at resume so a job that died
    mid-wait is distinguishable from one still going. It has no default because a caller
    that has not thought about the dead-job branch should have to say so out loud —
    passing ``None`` is allowed and flagged ``ST-W009``. ``pause_count`` increments on
    every pause, so a wait that never converges trips ``ST-E010`` even without a probe.
    """

    assert watchdog_active, (
        "pause_step requires an active idle watchdog on the VM; pausing without one is the banned "
        "fire-and-forget pattern (LESSONS Lesson 8)."
    )
    location: StepLocation = _locate(task_id=task_id, step_number=step_number)

    location.step[STATUS_FIELD] = STATUS_PAUSED_WAITING
    location.step[CURRENT_OWNER_FIELD] = None
    location.step[RESUME_SENTINEL_FIELD] = resume_sentinel
    location.step[PAUSED_AT_FIELD] = now_iso8601_utc()
    location.step[RESUME_AFTER_FIELD] = resume_after
    location.step[WATCHDOG_ACTIVE_FIELD] = watchdog_active
    # Normalize at the writer: the spec types this field `string | null`, and `""` on
    # disk would make every reader re-derive "is there a probe" for itself.
    location.step[LIVENESS_PROBE_FIELD] = (
        liveness_probe.strip()
        if liveness_probe is not None and len(liveness_probe.strip()) > 0
        else None
    )
    location.step[PAUSE_COUNT_FIELD] = _next_pause_count(step=location.step)

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
    pause_p.add_argument(
        "--liveness-probe",
        type=str,
        default=None,
        help=(
            "Shell command that exits 0 while the remote work is still running "
            "(e.g. 'ssh HOST tmux has-session -t train'). Without it a dead job is "
            "indistinguishable from a slow one and the step re-pauses forever."
        ),
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
            liveness_probe=args.liveness_probe,
        )
    else:
        parser.error(f"unknown action: {args.action}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
