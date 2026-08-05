"""Step-liveness verificator.

Inspects ``step_tracker.json`` for each task and classifies ``in_progress`` and
``paused_waiting`` steps into the liveness conditions below:

* ``ST-E007`` — stale heartbeat AND a live VM is provisioned for the task. This
  is an emergency: real money is burning while nothing is happening.
* ``ST-W005`` — stale heartbeat AND no live VM. The step is ghosted, but no
  billing risk.
* ``ST-W006`` — fresh heartbeat but elapsed wall-clock exceeds the expected
  duration scaled by ``slow_factor``. The step is alive but pathologically
  slow (verifier loop, polling loop, retry storm, …).
* ``ST-E008`` — a ``paused_waiting`` step whose ``watchdog_active`` is not
  ``true``. Pausing a step (ending the session to resume later from files) is
  only safe when the VM carries an idle dead-man's-switch watchdog; without it
  a missed resume burns idle billing — the banned fire-and-forget pattern.
* ``ST-E009`` — an ``in_progress`` step on a tracker that declares a
  ``spec_version`` is missing a liveness field, or carries one of the wrong
  type. No staleness check can fire for such a step, so skipping it silently
  would hide exactly the runaway this verificator exists to catch.
* ``ST-W008`` — the task has a live VM that does not record
  ``watchdog_active: true``. Detection only helps while somebody is awake to
  read it; the on-machine watchdog is what bounds spend when nobody is.
* ``ST-E010`` — a ``paused_waiting`` step has re-paused more than
  ``max_pause_count`` times. The wait is not converging: the step keeps going
  back to sleep waiting for something that is not coming.
* ``ST-W009`` — a ``paused_waiting`` step records no ``liveness_probe``, so a
  job that died mid-wait cannot be told from one still running and the step
  re-pauses forever instead of failing.

A ``paused_waiting`` step that is watchdog-protected, carries a probe, and has a
converging pause count is a deliberate, safe wait and is never flagged. The three
pause codes are independent, so one step can raise several at once.

"Stale heartbeat" is defined as
``(now - last_heartbeat_at) > heartbeat_interval_seconds * stale_multiplier``,
where ``stale_multiplier`` defaults to 3.

A machine counts as live when its ``machine_log.json`` entry has a non-empty
``instance_id`` and no ``destroyed_at`` — the definition in
``arf/specifications/step_tracker_specification.md``. Never detect liveness from
a provider status field such as Vast.ai's ``actual_status``: it describes a live
provider API response, is never persisted into ``machine_log.json``, and reading
it from the log classifies every machine as destroyed — silently downgrading the
``ST-E007`` idle-billing error to an ``ST-W005`` warning and exiting ``0`` while
a GPU bills.

Steps on a tracker with no ``spec_version`` at all (v1) that lack the liveness
fields are silently skipped, for backward compatibility.

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
from dataclasses import dataclass
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
# A step that has gone back to sleep a dozen times is not waiting, it is stuck: the
# thing it re-checks is never going to become true without a human.
DEFAULT_MAX_PAUSE_COUNT: int = 12

STATUS_FIELD: str = "status"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_PAUSED_WAITING: str = "paused_waiting"
WATCHDOG_ACTIVE_FIELD: str = "watchdog_active"
STARTED_AT_FIELD: str = "started_at"
LAST_HEARTBEAT_AT_FIELD: str = "last_heartbeat_at"
HEARTBEAT_INTERVAL_FIELD: str = "heartbeat_interval_seconds"
EXPECTED_COMPLETION_FIELD: str = "expected_completion_at"
STEP_FIELD: str = "step"
STEPS_FIELD: str = "steps"
SPEC_VERSION_FIELD: str = "spec_version"
LIVENESS_PROBE_FIELD: str = "liveness_probe"
PAUSE_COUNT_FIELD: str = "pause_count"
MACHINE_LOG_FILENAME: str = "machine_log.json"
SETUP_MACHINES_GLOB: str = "*setup-machines*"
MACHINE_INSTANCE_ID_FIELD: str = "instance_id"
MACHINE_DESTROYED_AT_FIELD: str = "destroyed_at"

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
CODE_ST_E009: DiagnosticCode = DiagnosticCode(
    prefix=PREFIX,
    severity=Severity.ERROR,
    number=9,
)
CODE_ST_W008: DiagnosticCode = DiagnosticCode(
    prefix=PREFIX,
    severity=Severity.WARNING,
    number=8,
)
CODE_ST_E010: DiagnosticCode = DiagnosticCode(
    prefix=PREFIX,
    severity=Severity.ERROR,
    number=10,
)
CODE_ST_W009: DiagnosticCode = DiagnosticCode(
    prefix=PREFIX,
    severity=Severity.WARNING,
    number=9,
)


@dataclass(frozen=True, slots=True)
class MachineLiveness:
    has_live_vm: bool
    has_unprotected_live_vm: bool


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


def _is_live_machine(*, entry: dict[str, object]) -> bool:
    # A machine is live when it was actually created and nothing has recorded its
    # destruction. Both fields are guaranteed by the machine-log schema; a provider
    # status field such as Vast.ai's `actual_status` is never persisted there, so
    # matching on one silently reports every machine as destroyed.
    instance_id: object = entry.get(MACHINE_INSTANCE_ID_FIELD)
    if not isinstance(instance_id, str) or len(instance_id) == 0:
        return False
    return entry.get(MACHINE_DESTROYED_AT_FIELD) is None


def _machine_liveness(*, task_id: str) -> MachineLiveness:
    steps_dir: Path = paths.step_logs_dir(task_id=task_id)
    if not steps_dir.exists():
        return MachineLiveness(has_live_vm=False, has_unprotected_live_vm=False)

    has_live_vm: bool = False
    has_unprotected_live_vm: bool = False
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
            if not _is_live_machine(entry=entry):
                continue
            has_live_vm = True
            if entry.get(WATCHDOG_ACTIVE_FIELD) is not True:
                has_unprotected_live_vm = True
    return MachineLiveness(
        has_live_vm=has_live_vm,
        has_unprotected_live_vm=has_unprotected_live_vm,
    )


def _classify_step(
    *,
    step: dict[str, object],
    has_live_vm: bool,
    tracker_declares_spec_version: bool,
    now: datetime,
    stale_multiplier: float,
    slow_factor: float,
) -> DiagnosticCode | None:
    last_heartbeat_obj: object = step.get(LAST_HEARTBEAT_AT_FIELD)
    interval_obj: object = step.get(HEARTBEAT_INTERVAL_FIELD)
    expected_completion_obj: object = step.get(EXPECTED_COMPLETION_FIELD)

    # A step missing any liveness field can never be classified as ghosted or slow.
    # On a v2+ tracker that is an error, not something to skip: silence here is what
    # lets an unattended step keep a GPU billing without a single diagnostic.
    unmonitorable: DiagnosticCode | None = CODE_ST_E009 if tracker_declares_spec_version else None

    if not isinstance(last_heartbeat_obj, str):
        return unmonitorable
    if not isinstance(interval_obj, int) or isinstance(interval_obj, bool):
        return unmonitorable
    if not isinstance(expected_completion_obj, str):
        return unmonitorable

    try:
        last_heartbeat: datetime = _parse_iso8601(value=last_heartbeat_obj)
        expected_completion: datetime = _parse_iso8601(value=expected_completion_obj)
    except ValueError:
        return unmonitorable

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


def _classify_paused_step(
    *,
    step: dict[str, object],
    max_pause_count: int,
) -> list[DiagnosticCode]:
    """Every code a ``paused_waiting`` step can raise.

    A list, not a single code: a pause can be unprotected, non-converging, and blind at
    the same time, and reporting only the first would hide the other two behind a fix for
    the one.
    """

    codes: list[DiagnosticCode] = []
    if step.get(WATCHDOG_ACTIVE_FIELD) is not True:
        # Safe to leave ONLY if the VM carries an idle watchdog; otherwise this is the
        # banned fire-and-forget pattern.
        codes.append(CODE_ST_E008)

    pause_count: object = step.get(PAUSE_COUNT_FIELD)
    if (
        isinstance(pause_count, int)
        and not isinstance(pause_count, bool)
        and pause_count > max_pause_count
    ):
        codes.append(CODE_ST_E010)

    probe: object = step.get(LIVENESS_PROBE_FIELD)
    if not isinstance(probe, str) or len(probe.strip()) == 0:
        codes.append(CODE_ST_W009)

    return codes


def _build_message(
    *,
    code: DiagnosticCode,
    task_id: str,
    step_number: int | None,
    last_heartbeat_at: object,
    started_at: object,
    expected_completion_at: object,
) -> str:
    # A malformed tracker is exactly what this verificator surfaces, so say the step
    # number is unreadable rather than printing a sentinel that looks like a real one.
    step_label: str = str(step_number) if step_number is not None else "<unreadable>"
    if code == CODE_ST_E008:
        return (
            f"Task {task_id} step {step_label}: status is 'paused_waiting' but "
            f"watchdog_active is not true — pausing a step without an active VM idle "
            f"watchdog is unsafe (a missed resume burns idle billing, LESSONS Lesson 8). "
            f"Drive the step synchronously instead, or confirm the watchdog is installed."
        )
    if code == CODE_ST_E010:
        return (
            f"Task {task_id} step {step_label}: status is 'paused_waiting' and it has "
            f"re-paused more times than the cap allows — the wait is not converging, so "
            f"something is being waited on that will not arrive. Run "
            f"arf.scripts.utils.resume_check, then drive the step to a terminal state or "
            f"transition it to blocked_intervention. Do not pause it again."
        )
    if code == CODE_ST_W009:
        return (
            f"Task {task_id} step {step_label}: status is 'paused_waiting' with no "
            f"liveness_probe — a job that died mid-wait cannot be told apart from one "
            f"still running, so this step will re-pause forever instead of failing. "
            f"Record a probe when pausing (heartbeat pause --liveness-probe ...)."
        )
    if code == CODE_ST_E009:
        return (
            f"Task {task_id} step {step_label}: status is 'in_progress' but a liveness "
            f"field is missing or malformed (last_heartbeat_at={last_heartbeat_at!r}, "
            f"expected_completion_at={expected_completion_at!r}) — the step cannot be "
            f"monitored, so no staleness check can ever fire for it. Adopt it via "
            f"arf.scripts.utils.heartbeat.start_step, or move it to a terminal state."
        )
    if code == CODE_ST_E007:
        return (
            f"Task {task_id} step {step_label}: stale heartbeat "
            f"(last_heartbeat_at={last_heartbeat_at!r}) AND a live VM is "
            f"provisioned — emergency, run /diagnose-stuck-step and stop the VM."
        )
    if code == CODE_ST_W005:
        return (
            f"Task {task_id} step {step_label}: stale heartbeat "
            f"(last_heartbeat_at={last_heartbeat_at!r}) — owner has ghosted "
            f"the step; transition it to completed or blocked_intervention."
        )
    if code == CODE_ST_W006:
        return (
            f"Task {task_id} step {step_label}: heartbeat fresh but elapsed "
            f"wall-clock exceeds expected_completion_at "
            f"({expected_completion_at!r}, started_at={started_at!r}) by the "
            f"configured slow factor — investigate why it is slow."
        )
    # A code with no branch above would otherwise inherit the ST-W006 wording and point
    # the operator at heartbeat fields the step may not even have.
    raise AssertionError(f"code is a classified liveness code, got {code}")


def verify_step_liveness(
    *,
    task_id: str | None = None,
    now: datetime | None = None,
    stale_multiplier: float = DEFAULT_STALE_MULTIPLIER,
    slow_factor: float = DEFAULT_SLOW_FACTOR,
    max_pause_count: int = DEFAULT_MAX_PAUSE_COUNT,
) -> VerificationResult | list[VerificationResult]:
    assert max_pause_count >= 1, "max_pause_count allows at least one pause"
    resolved_now: datetime = now if now is not None else datetime.now(tz=UTC)

    if task_id is not None:
        return _verify_one(
            task_id=task_id,
            now=resolved_now,
            stale_multiplier=stale_multiplier,
            slow_factor=slow_factor,
            max_pause_count=max_pause_count,
        )

    return [
        _verify_one(
            task_id=candidate_id,
            now=resolved_now,
            stale_multiplier=stale_multiplier,
            slow_factor=slow_factor,
            max_pause_count=max_pause_count,
        )
        for candidate_id in _list_task_ids()
    ]


def _verify_one(
    *,
    task_id: str,
    now: datetime,
    stale_multiplier: float,
    slow_factor: float,
    max_pause_count: int,
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

    liveness: MachineLiveness = _machine_liveness(task_id=task_id)
    tracker_declares_spec_version: bool = tracker_data.get(SPEC_VERSION_FIELD) is not None

    if liveness.has_unprotected_live_vm:
        result.diagnostics.append(
            Diagnostic(
                code=CODE_ST_W008,
                message=(
                    f"Task {task_id}: a live VM does not record watchdog_active: true — "
                    f"nothing on the machine can stop it billing if this session goes "
                    f"away. Install the idle watchdog (/setup-remote-machine Phase 3) and "
                    f"record watchdog_active in machine_log.json."
                ),
                file_path=tracker_path,
            ),
        )

    for entry in steps_obj:
        if not isinstance(entry, dict):
            continue
        status_obj: object = entry.get(STATUS_FIELD)
        codes: list[DiagnosticCode] = []
        if status_obj == STATUS_IN_PROGRESS:
            in_progress_code: DiagnosticCode | None = _classify_step(
                step=entry,
                has_live_vm=liveness.has_live_vm,
                tracker_declares_spec_version=tracker_declares_spec_version,
                now=now,
                stale_multiplier=stale_multiplier,
                slow_factor=slow_factor,
            )
            if in_progress_code is not None:
                codes.append(in_progress_code)
        elif status_obj == STATUS_PAUSED_WAITING:
            # A paused step with an active watchdog, a probe, and a converging pause count
            # is a deliberate, safe wait and raises nothing.
            codes = _classify_paused_step(step=entry, max_pause_count=max_pause_count)
        if len(codes) == 0:
            continue
        step_number_obj: object = entry.get(STEP_FIELD)
        # `isinstance(True, int)` is True, so an unguarded check turns `"step": true`
        # into step 1 and blames a step that is not the broken one.
        step_number: int | None = (
            step_number_obj
            if isinstance(step_number_obj, int) and not isinstance(step_number_obj, bool)
            else None
        )
        for code in codes:
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
    parser.add_argument(
        "--max-pause-count",
        type=int,
        default=DEFAULT_MAX_PAUSE_COUNT,
        help="How many times a step may re-pause before it counts as not converging.",
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
        max_pause_count=args.max_pause_count,
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
