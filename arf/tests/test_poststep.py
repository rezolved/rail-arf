"""Tests for poststep skipped-step log warnings and terminal-state liveness finalization.

Per ``arf/specifications/step_tracker_specification.md`` v5, "Who writes the liveness
fields": ``poststep`` finalizes a step by clearing ``current_owner`` and writing
``actual_duration_seconds`` when the step reaches a terminal state.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import arf.scripts.utils.poststep as poststep_module
import arf.scripts.utils.prestep as prestep_module
from arf.scripts.verificators.common import paths
from arf.tests.fixtures.log_builders import build_step_log
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.task_builder import (
    build_step_tracker,
    build_task_folder,
    build_task_json,
)

TASK_ID: str = "t0001_test"


def _setup(*, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=repo_root,
        verificator_modules=[poststep_module],
    )


def _build_tracker(
    *,
    steps: list[dict[str, object]],
) -> dict[str, object]:
    return {"steps": steps}


def _make_step(
    *,
    step: int,
    name: str,
    status: str,
) -> dict[str, object]:
    return {"step": step, "name": name, "status": status}


# ---------------------------------------------------------------------------
# Skipped-step log warnings
# ---------------------------------------------------------------------------


def test_no_warning_when_skipped_step_has_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
    build_step_log(
        repo_root=tmp_path,
        task_id=TASK_ID,
        step_order=3,
        step_id="research-papers",
        status="skipped",
    )
    tracker: dict[str, object] = _build_tracker(
        steps=[
            _make_step(step=3, name="research-papers", status="skipped"),
            _make_step(step=5, name="implementation", status="in_progress"),
        ],
    )
    poststep_module._warn_missing_skipped_step_logs(
        task_id=TASK_ID,
        tracker=tracker,
        current_step_order=5,
    )
    captured: str = capsys.readouterr().out
    assert "WARNING" not in captured


def test_warning_when_skipped_step_missing_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
    tracker: dict[str, object] = _build_tracker(
        steps=[
            _make_step(step=3, name="research-papers", status="skipped"),
            _make_step(step=5, name="implementation", status="in_progress"),
        ],
    )
    poststep_module._warn_missing_skipped_step_logs(
        task_id=TASK_ID,
        tracker=tracker,
        current_step_order=5,
    )
    captured: str = capsys.readouterr().out
    assert "WARNING" in captured
    assert "research-papers" in captured


def test_no_warning_for_skipped_step_after_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
    tracker: dict[str, object] = _build_tracker(
        steps=[
            _make_step(step=3, name="implementation", status="in_progress"),
            _make_step(step=8, name="suggestions", status="skipped"),
        ],
    )
    poststep_module._warn_missing_skipped_step_logs(
        task_id=TASK_ID,
        tracker=tracker,
        current_step_order=3,
    )
    captured: str = capsys.readouterr().out
    assert "WARNING" not in captured


def test_no_warning_for_completed_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
    tracker: dict[str, object] = _build_tracker(
        steps=[
            _make_step(step=3, name="research-papers", status="completed"),
            _make_step(step=5, name="implementation", status="in_progress"),
        ],
    )
    poststep_module._warn_missing_skipped_step_logs(
        task_id=TASK_ID,
        tracker=tracker,
        current_step_order=5,
    )
    captured: str = capsys.readouterr().out
    assert "WARNING" not in captured


# ---------------------------------------------------------------------------
# Liveness finalization: prestep arms -> poststep finalizes
# ---------------------------------------------------------------------------

ROUND_TRIP_STEP_ID: str = "research-papers"
ROUND_TRIP_STEP_NUMBER: int = 4
TASK_BRANCH: str = f"task/{TASK_ID}"

STEPS_FIELD: str = "steps"
STEP_FIELD: str = "step"
NAME_FIELD: str = "name"
STATUS_FIELD: str = "status"
STARTED_AT_FIELD: str = "started_at"
COMPLETED_AT_FIELD: str = "completed_at"
LOG_FILE_FIELD: str = "log_file"
CURRENT_OWNER_FIELD: str = "current_owner"
ACTUAL_DURATION_FIELD: str = "actual_duration_seconds"

STATUS_PENDING: str = "pending"
STATUS_COMPLETED: str = "completed"


GIT_ADD_COMMAND: list[str] = ["git", "add"]
GIT_COMMIT_COMMAND: list[str] = ["git", "commit"]
COMMIT_SUCCESS_MESSAGE: str = "Committed step_tracker.json update"
FAILED_STDOUT_MARKER: str = "GIT-STDOUT-MARKER"
FAILED_STDERR_MARKER: str = "GIT-STDERR-MARKER"


def _fake_subprocess_run(
    args: list[str],
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def _make_failing_subprocess_run(
    *,
    failing_command: list[str],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if list(args[: len(failing_command)]) == failing_command:
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout=FAILED_STDOUT_MARKER,
                stderr=FAILED_STDERR_MARKER,
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    return fake_run


def _setup_round_trip(
    *,
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = _fake_subprocess_run,
) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=repo_root,
        verificator_modules=[poststep_module, prestep_module],
    )
    # Both scripts gate on git state and shell out to verificators; neither is
    # relevant to the liveness contract under test.
    for module in [poststep_module, prestep_module]:
        monkeypatch.setattr(module, "_detect_repo_root", lambda: repo_root)
        monkeypatch.setattr(module, "_is_working_tree_clean", lambda *, repo_root: True)
    monkeypatch.setattr(
        prestep_module,
        "_get_current_branch",
        lambda *, repo_root: TASK_BRANCH,
    )
    monkeypatch.setattr(
        poststep_module,
        "_get_latest_commit_message",
        lambda *, repo_root: f"{TASK_ID} [{ROUND_TRIP_STEP_ID}]: Do the work",
    )
    monkeypatch.setattr(subprocess, "run", subprocess_run)


def _round_trip_steps() -> list[dict[str, object]]:
    earlier: list[dict[str, object]] = [
        {
            STEP_FIELD: number,
            NAME_FIELD: f"earlier-step-{number}",
            "description": f"Completed step {number}.",
            STATUS_FIELD: STATUS_COMPLETED,
            STARTED_AT_FIELD: "2026-04-01T00:00:00Z",
            COMPLETED_AT_FIELD: "2026-04-01T00:10:00Z",
            LOG_FILE_FIELD: f"logs/steps/{number:03d}_earlier-step-{number}/",
        }
        for number in range(1, ROUND_TRIP_STEP_NUMBER)
    ]
    return [
        *earlier,
        {
            STEP_FIELD: ROUND_TRIP_STEP_NUMBER,
            NAME_FIELD: ROUND_TRIP_STEP_ID,
            "description": "Review the paper corpus.",
            STATUS_FIELD: STATUS_PENDING,
            STARTED_AT_FIELD: None,
            COMPLETED_AT_FIELD: None,
            LOG_FILE_FIELD: None,
        },
    ]


def _read_round_trip_step() -> dict[str, object]:
    tracker_path: Path = paths.step_tracker_path(task_id=TASK_ID)
    tracker: object = json.loads(tracker_path.read_text(encoding="utf-8"))
    assert isinstance(tracker, dict), "tracker root is a JSON object"
    steps: object = tracker[STEPS_FIELD]
    assert isinstance(steps, list), "steps is a list"
    for step in steps:
        assert isinstance(step, dict), "step entry is a dict"
        if step[STEP_FIELD] == ROUND_TRIP_STEP_NUMBER:
            return step
    raise AssertionError(f"step {ROUND_TRIP_STEP_NUMBER} not found in tracker")


def test_poststep_finalizes_liveness_after_prestep_arms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup_round_trip(monkeypatch=monkeypatch, repo_root=tmp_path)
    build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
    build_task_json(repo_root=tmp_path, task_id=TASK_ID)
    build_step_tracker(repo_root=tmp_path, task_id=TASK_ID, steps=_round_trip_steps())

    arm_code: int = prestep_module.run_prestep(
        task_id=TASK_ID,
        step_id=ROUND_TRIP_STEP_ID,
    )
    assert arm_code == 0, "prestep must arm the step"
    armed_step: dict[str, object] = _read_round_trip_step()
    assert armed_step[CURRENT_OWNER_FIELD] is not None, "the armed step has a live owner"

    finalize_code: int = poststep_module.run_poststep(
        task_id=TASK_ID,
        step_id=ROUND_TRIP_STEP_ID,
    )
    assert finalize_code == 0, "poststep must finalize the step"

    step: dict[str, object] = _read_round_trip_step()
    assert step[STATUS_FIELD] == STATUS_COMPLETED
    assert step[CURRENT_OWNER_FIELD] is None, "a terminal step has no live owner"
    duration: object = step[ACTUAL_DURATION_FIELD]
    assert isinstance(duration, int), "actual_duration_seconds is an int"
    assert duration >= 0


# ---------------------------------------------------------------------------
# The auto-commit of the finalized tracker must be checked, not fired and forgotten
# ---------------------------------------------------------------------------


def _build_round_trip_task(*, repo_root: Path) -> None:
    build_task_folder(repo_root=repo_root, task_id=TASK_ID)
    build_task_json(repo_root=repo_root, task_id=TASK_ID)
    build_step_tracker(repo_root=repo_root, task_id=TASK_ID, steps=_round_trip_steps())
    arm_code: int = prestep_module.run_prestep(
        task_id=TASK_ID,
        step_id=ROUND_TRIP_STEP_ID,
    )
    assert arm_code == 0, "prestep must arm the step"


@pytest.mark.parametrize(
    "failing_command",
    [
        GIT_ADD_COMMAND,
        GIT_COMMIT_COMMAND,
    ],
)
def test_poststep_fails_when_tracker_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing_command: list[str],
) -> None:
    # An unchecked commit reports the step completed while the tracker update never
    # lands, so the next wakeup reads a step nobody finalized.
    _setup_round_trip(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        subprocess_run=_make_failing_subprocess_run(failing_command=failing_command),
    )
    _build_round_trip_task(repo_root=tmp_path)

    exit_code: int = poststep_module.run_poststep(
        task_id=TASK_ID,
        step_id=ROUND_TRIP_STEP_ID,
    )

    assert exit_code == 1, f"a failing `{' '.join(failing_command)}` must fail poststep"
    captured = capsys.readouterr()
    combined: str = captured.out + captured.err
    assert COMMIT_SUCCESS_MESSAGE not in combined, (
        "poststep must not claim it committed when the command failed"
    )
    assert FAILED_STDOUT_MARKER in combined, "the failing command's stdout must be surfaced"
    assert FAILED_STDERR_MARKER in combined, "the failing command's stderr must be surfaced"


def test_poststep_returns_zero_when_tracker_commit_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_round_trip(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_round_trip_task(repo_root=tmp_path)

    exit_code: int = poststep_module.run_poststep(
        task_id=TASK_ID,
        step_id=ROUND_TRIP_STEP_ID,
    )

    assert exit_code == 0
    assert COMMIT_SUCCESS_MESSAGE in capsys.readouterr().out
