"""Tell a still-running remote job from a dead one before resuming a paused step.

A `paused_waiting` step records a `resume_sentinel` — what to look for when the wait is
over. A job that dies mid-wait never produces that sentinel, so a resume that only checks
the sentinel re-pauses, and re-pauses, and never reports the run died. The watchdog stops
the VM an hour later, which bounds the money and hides the symptom: the task reads as
"waiting" forever on work that no longer exists.

This script runs the `liveness_probe` recorded by `heartbeat.pause_step` and reports which
of three branches applies, so the resume decision is a script's output rather than a
skill's judgement:

* `no_probe` (exit 0) — the pause recorded no probe (a pre-v6 pause). The caller falls
  back to checking the sentinel by hand.
* `job_alive` (exit 0) — the probe succeeded; pause again with a new `resume_after`.
* `job_dead` (exit 3) — the probe failed; do NOT pause again. Collect the job log,
  transition the step to `failed` or `blocked_intervention`, and write an intervention
  file.

A probe that times out counts as **alive**. A hung SSH is not evidence that a job died,
and the cost of guessing wrong in that direction is aborting a healthy multi-hour run.

See "A pause must be able to end in failure" in
`arf/specifications/step_tracker_specification.md`.

CLI form:

```text
python -m arf.scripts.utils.resume_check <task_id> <step_number> [--timeout-seconds 120]
```
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from arf.scripts.verificators.common import paths

STATUS_FIELD: str = "status"
STATUS_PAUSED_WAITING: str = "paused_waiting"
STEPS_FIELD: str = "steps"
STEP_FIELD: str = "step"
LIVENESS_PROBE_FIELD: str = "liveness_probe"
PAUSE_COUNT_FIELD: str = "pause_count"

DECISION_KEY: str = "decision"
PROBE_KEY: str = "probe"
RETURNCODE_KEY: str = "returncode"
PAUSE_COUNT_KEY: str = "pause_count"


class Decision(StrEnum):
    """The three branches a resume can take. Closed on purpose.

    The exit code this script returns is its whole product, and a bare-string decision
    lets a fourth branch added later fall through to "not dead, so exit 0" silently.
    """

    NO_PROBE = "no_probe"
    JOB_ALIVE = "job_alive"
    JOB_DEAD = "job_dead"


EXIT_OK: int = 0
EXIT_NOT_PAUSED: int = 1
EXIT_JOB_DEAD: int = 3

DEFAULT_TIMEOUT_SECONDS: float = 120.0


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    decision: Decision
    returncode: int | None


def run_liveness_probe(*, probe: str, timeout_seconds: float) -> ProbeOutcome:
    """Run one probe command and classify the result.

    Pure with respect to the tracker — takes the command, returns the branch — so the
    decision logic is testable without a task folder.
    """

    assert len(probe.strip()) > 0, "probe is a non-empty command"
    assert timeout_seconds > 0.0, "probe timeout is positive"

    try:
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            probe,
            shell=True,  # noqa: S602 - the probe is an operator-authored shell command by design
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Alive, deliberately: a probe that hangs says the network or the box is slow,
        # not that the work stopped. Calling this dead would kill healthy runs.
        return ProbeOutcome(decision=Decision.JOB_ALIVE, returncode=None)
    if completed.returncode == 0:
        return ProbeOutcome(decision=Decision.JOB_ALIVE, returncode=completed.returncode)
    return ProbeOutcome(decision=Decision.JOB_DEAD, returncode=completed.returncode)


def _find_step(*, tracker: dict[str, object], step_number: int) -> dict[str, object] | None:
    steps_obj: object = tracker.get(STEPS_FIELD)
    if not isinstance(steps_obj, list):
        return None
    for entry in steps_obj:
        if not isinstance(entry, dict):
            continue
        if entry.get(STEP_FIELD) == step_number:
            return entry
    return None


def _pause_count(*, step: dict[str, object]) -> int:
    value: object = step.get(PAUSE_COUNT_FIELD)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _emit(
    *,
    decision: Decision,
    probe: str | None,
    returncode: int | None,
    pause_count: int,
) -> None:
    payload: dict[str, object] = {
        DECISION_KEY: decision.value,
        PROBE_KEY: probe,
        RETURNCODE_KEY: returncode,
        PAUSE_COUNT_KEY: pause_count,
    }
    print(json.dumps(payload))


def _build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="arf.scripts.utils.resume_check",
        description="Report whether a paused step's remote job is still alive.",
    )
    parser.add_argument("task_id", type=str)
    parser.add_argument("step_number", type=int)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="How long to wait for the probe before treating it as alive.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = _build_parser()
    args: argparse.Namespace = parser.parse_args(argv)

    tracker_path: Path = paths.step_tracker_path(task_id=args.task_id)
    if not tracker_path.exists():
        sys.stderr.write(f"step_tracker.json not found for task {args.task_id} at {tracker_path}\n")
        return EXIT_NOT_PAUSED

    try:
        tracker: object = json.loads(tracker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        sys.stderr.write(f"could not read {tracker_path}: {error}\n")
        return EXIT_NOT_PAUSED

    if not isinstance(tracker, dict):
        sys.stderr.write(f"{tracker_path} is not a JSON object\n")
        return EXIT_NOT_PAUSED

    step: dict[str, object] | None = _find_step(tracker=tracker, step_number=args.step_number)
    if step is None:
        sys.stderr.write(f"step {args.step_number} not found in {tracker_path}\n")
        return EXIT_NOT_PAUSED

    status: object = step.get(STATUS_FIELD)
    if status != STATUS_PAUSED_WAITING:
        sys.stderr.write(
            f"step {args.step_number} of {args.task_id} has status {status!r}, not "
            f"{STATUS_PAUSED_WAITING!r} — there is no pause to resume from.\n",
        )
        return EXIT_NOT_PAUSED

    pause_count: int = _pause_count(step=step)
    probe_obj: object = step.get(LIVENESS_PROBE_FIELD)
    if not isinstance(probe_obj, str) or len(probe_obj.strip()) == 0:
        _emit(
            decision=Decision.NO_PROBE,
            probe=None,
            returncode=None,
            pause_count=pause_count,
        )
        return EXIT_OK

    outcome: ProbeOutcome = run_liveness_probe(
        probe=probe_obj,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(
        decision=outcome.decision,
        probe=probe_obj,
        returncode=outcome.returncode,
        pause_count=pause_count,
    )
    if outcome.decision is Decision.JOB_DEAD:
        return EXIT_JOB_DEAD
    if outcome.decision is Decision.JOB_ALIVE:
        return EXIT_OK
    if outcome.decision is Decision.NO_PROBE:
        return EXIT_OK
    assert_never(outcome.decision)


if __name__ == "__main__":
    sys.exit(main())
