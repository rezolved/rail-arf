"""Step-liveness verificator.

Inspects ``step_tracker.json`` for each task and classifies ``in_progress``
steps into one of three liveness conditions:

* ``ST-E007`` — stale heartbeat AND a live VM is provisioned for the task
  (a ``machine_log.json`` entry with ``actual_status == "running"``). This is
  an emergency: real money is burning while nothing is happening.
* ``ST-W005`` — stale heartbeat AND no live VM. The step is ghosted, but no
  billing risk.
* ``ST-W006`` — fresh heartbeat but elapsed wall-clock exceeds the expected
  duration scaled by ``slow_factor``. The step is alive but pathologically
  slow (verifier loop, polling loop, retry storm, …).
* ``ST-E008`` — a ``paused_waiting`` step whose ``watchdog_active`` is not
  ``true``. Pausing a step (ending the session to resume later from files) is
  only safe when the VM carries an idle dead-man's-switch watchdog; without it
  a missed resume burns idle billing — the banned fire-and-forget pattern.

A ``paused_waiting`` step with ``watchdog_active == true`` is a deliberate, safe
wait and is never flagged (it is not ghosted).

"Stale heartbeat" is defined as
``(now - last_heartbeat_at) > heartbeat_interval_seconds * stale_multiplier``,
where ``stale_multiplier`` defaults to 3.

Steps that lack the v2 liveness fields are silently skipped (backward
compatibility with v1 trackers).

Usage:

```text
python -m arf.scripts.verificators.verify_step_liveness <task_id>
python -m arf.scripts.verificators.verify_step_liveness --all
python -m arf.scripts.verificators.verify_step_liveness --all --slow-factor 3.0
```

Exit codes:

* ``0`` — no errors (warnings may be present).
* ``1`` — at least one error.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from arf.scripts.verificators.common import paths
from arf.scripts.verificators.common.reporting import print_verification_result
from arf.scripts.verificators.common.types import (
    Diagnostic,
    DiagnosticCode,
    Severity,
    VerificationResult,
)

PREFIX: str = "ST"
DEFAULT_STALE_MULTIPLIER: float = 3.0
DEFAULT_SLOW_FACTOR: float = 2.0

STATUS_FIELD: str = "status"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_PAUSED_WAITING: str = "paused_waiting"
WATCHDOG_ACTIVE_FIELD: str = "watchdog_active"
RESUME_AFTER_FIELD: str = "resume_after"
STARTED_AT_FIELD: str = "started_at"
LAST_HEARTBEAT_AT_FIELD: str = "last_heartbeat_at"
HEARTBEAT_INTERVAL_FIELD: str = "heartbeat_interval_seconds"
EXPECTED_COMPLETION_FIELD: str = "expected_completion_at"
STEP_FIELD: str = "step"
STEPS_FIELD: str = "steps"
MACHINE_LOG_FILENAME: str = "machine_log.json"
SETUP_MACHINES_GLOB: str = "*setup-machines*"
MACHINE_ACTUAL_STATUS_FIELD: str = "actual_status"
MACHINE_RUNNING_STATUS: str = "running"

CODE_ST_E007: DiagnosticCode = DiagnosticCode(
    prefix=PREFIX,
    severity=Severity.ERROR,
    number=7,
)
CODE_ST_W005: DiagnosticCode = DiagnosticCode(
    prefix=PREFIX,
    severity=Severity.WARNING,
    number=5,
)
CODE_ST_W006: DiagnosticCode = DiagnosticCode(
    prefix=PREFIX,
    severity=Severity.WARNING,
    number=6,
)
CODE_ST_E008: DiagnosticCode = DiagnosticCode(
    prefix=PREFIX,
    severity=Severity.ERROR,
    number=8,
)


def _parse_iso8601(*, value: str) -> datetime:
    normalized: str = value.replace("Z", "+00:00")
    parsed: datetime = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _list_task_ids() -> list[str]:
    if not paths.TASKS_DIR.exists():
        return []
    return sorted(
        d.name
        for d in paths.TASKS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("__")
    )


def _has_live_machine(*, task_id: str) -> bool:
    steps_dir: Path = paths.step_logs_dir(task_id=task_id)
    if not steps_dir.exists():
        return False
    for machine_log_path in steps_dir.glob(f"{SETUP_MACHINES_GLOB}/{MACHINE_LOG_FILENAME}"):
        try:
            raw: str = machine_log_path.read_text(encoding="utf-8")
            entries: object = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get(MACHINE_ACTUAL_STATUS_FIELD) == MACHINE_RUNNING_STATUS:
                return True
    return False


def _classify_step(
    *,
    step: dict[str, object],
    has_live_vm: bool,
    now: datetime,
    stale_multiplier: float,
    slow_factor: float,
) -> DiagnosticCode | None:
    last_heartbeat_obj: object = step.get(LAST_HEARTBEAT_AT_FIELD)
    interval_obj: object = step.get(HEARTBEAT_INTERVAL_FIELD)
    expected_completion_obj: object = step.get(EXPECTED_COMPLETION_FIELD)

    if not isinstance(last_heartbeat_obj, str):
        return None
    if not isinstance(interval_obj, int):
        return None
    if not isinstance(expected_completion_obj, str):
        return None

    try:
        last_heartbeat: datetime = _parse_iso8601(value=last_heartbeat_obj)
        expected_completion: datetime = _parse_iso8601(value=expected_completion_obj)
    except ValueError:
        return None

    heartbeat_age_seconds: float = (now - last_heartbeat).total_seconds()
    stale_threshold_seconds: float = float(interval_obj) * stale_multiplier
    if heartbeat_age_seconds > stale_threshold_seconds:
        return CODE_ST_E007 if has_live_vm else CODE_ST_W005

    started_at_obj: object = step.get(STARTED_AT_FIELD)
    if not isinstance(started_at_obj, str):
        return None
    try:
        started_at: datetime = _parse_iso8601(value=started_at_obj)
    except ValueError:
        return None
    expected_duration_seconds: float = (expected_completion - started_at).total_seconds()
    if expected_duration_seconds <= 0:
        return None
    slow_threshold: datetime = expected_completion + (
        (expected_completion - started_at) * (slow_factor - 1.0)
    )
    if now > slow_threshold:
        return CODE_ST_W006

    return None


def _build_message(
    *,
    code: DiagnosticCode,
    task_id: str,
    step_number: int,
    last_heartbeat_at: object,
    started_at: object,
    expected_completion_at: object,
) -> str:
    if code == CODE_ST_E008:
        return (
            f"Task {task_id} step {step_number}: status is 'paused_waiting' but "
            f"watchdog_active is not true — pausing a step without an active VM idle "
            f"watchdog is unsafe (a missed resume burns idle billing, LESSONS Lesson 8). "
            f"Drive the step synchronously instead, or confirm the watchdog is installed."
        )
    if code == CODE_ST_E007:
        return (
            f"Task {task_id} step {step_number}: stale heartbeat "
            f"(last_heartbeat_at={last_heartbeat_at!r}) AND a live VM is "
            f"provisioned — emergency, run /diagnose-stuck-step and stop the VM."
        )
    if code == CODE_ST_W005:
        return (
            f"Task {task_id} step {step_number}: stale heartbeat "
            f"(last_heartbeat_at={last_heartbeat_at!r}) — owner has ghosted "
            f"the step; transition it to completed or blocked_intervention."
        )
    return (
        f"Task {task_id} step {step_number}: heartbeat fresh but elapsed "
        f"wall-clock exceeds expected_completion_at "
        f"({expected_completion_at!r}, started_at={started_at!r}) by the "
        f"configured slow factor — investigate why it is slow."
    )


def verify_step_liveness(
    *,
    task_id: str | None = None,
    now: datetime | None = None,
    stale_multiplier: float = DEFAULT_STALE_MULTIPLIER,
    slow_factor: float = DEFAULT_SLOW_FACTOR,
) -> VerificationResult | list[VerificationResult]:
    resolved_now: datetime = now if now is not None else datetime.now(tz=UTC)

    if task_id is not None:
        return _verify_one(
            task_id=task_id,
            now=resolved_now,
            stale_multiplier=stale_multiplier,
            slow_factor=slow_factor,
        )

    return [
        _verify_one(
            task_id=candidate_id,
            now=resolved_now,
            stale_multiplier=stale_multiplier,
            slow_factor=slow_factor,
        )
        for candidate_id in _list_task_ids()
    ]


def _verify_one(
    *,
    task_id: str,
    now: datetime,
    stale_multiplier: float,
    slow_factor: float,
) -> VerificationResult:
    tracker_path: Path = paths.step_tracker_path(task_id=task_id)
    result: VerificationResult = VerificationResult(file_path=tracker_path, diagnostics=[])

    if not tracker_path.exists():
        return result

    try:
        raw: str = tracker_path.read_text(encoding="utf-8")
        tracker_data: object = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return result

    if not isinstance(tracker_data, dict):
        return result
    steps_obj: object = tracker_data.get(STEPS_FIELD)
    if not isinstance(steps_obj, list):
        return result

    has_live_vm: bool = _has_live_machine(task_id=task_id)

    for entry in steps_obj:
        if not isinstance(entry, dict):
            continue
        status_obj: object = entry.get(STATUS_FIELD)
        code: DiagnosticCode | None = None
        if status_obj == STATUS_IN_PROGRESS:
            code = _classify_step(
                step=entry,
                has_live_vm=has_live_vm,
                now=now,
                stale_multiplier=stale_multiplier,
                slow_factor=slow_factor,
            )
        elif status_obj == STATUS_PAUSED_WAITING and entry.get(WATCHDOG_ACTIVE_FIELD) is not True:
            # A deliberately paused step is safe to leave ONLY if its VM carries an idle
            # watchdog; otherwise it is the banned fire-and-forget pattern. A paused step with an
            # active watchdog is never flagged (it is not ghosted).
            code = CODE_ST_E008
        if code is None:
            continue
        step_number_obj: object = entry.get(STEP_FIELD)
        step_number: int = step_number_obj if isinstance(step_number_obj, int) else -1
        message: str = _build_message(
            code=code,
            task_id=task_id,
            step_number=step_number,
            last_heartbeat_at=entry.get(LAST_HEARTBEAT_AT_FIELD),
            started_at=entry.get(STARTED_AT_FIELD),
            expected_completion_at=entry.get(EXPECTED_COMPLETION_FIELD),
        )
        result.diagnostics.append(
            Diagnostic(
                code=code,
                message=message,
                file_path=tracker_path,
            ),
        )

    return result


def _parse_args() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="arf.scripts.verificators.verify_step_liveness",
        description="Detect ghosted or pathologically slow in_progress steps.",
    )
    parser.add_argument(
        "task_id",
        nargs="?",
        help="Task to inspect. Omit when --all is given.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan every task under tasks/.",
    )
    parser.add_argument(
        "--stale-multiplier",
        type=float,
        default=DEFAULT_STALE_MULTIPLIER,
        help="Multiplier on heartbeat_interval_seconds to consider a heartbeat stale.",
    )
    parser.add_argument(
        "--slow-factor",
        type=float,
        default=DEFAULT_SLOW_FACTOR,
        help="Factor on expected duration before flagging a slow step.",
    )
    return parser.parse_args()


def main() -> None:
    args: argparse.Namespace = _parse_args()

    if (not args.all) and (args.task_id is None):
        sys.stderr.write("Provide a task_id or pass --all.\n")
        sys.exit(2)

    task_id_arg: str | None = None if args.all else args.task_id
    outcome: VerificationResult | list[VerificationResult] = verify_step_liveness(
        task_id=task_id_arg,
        stale_multiplier=args.stale_multiplier,
        slow_factor=args.slow_factor,
    )

    results: list[VerificationResult] = outcome if isinstance(outcome, list) else [outcome]

    any_errors: bool = False
    for result in results:
        if len(result.diagnostics) > 0:
            print_verification_result(result=result)
        if not result.passed:
            any_errors = True

    if any_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
