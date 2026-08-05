"""Tests for ``arf.scripts.utils.resume_check``.

At resume time a paused step has to tell a still-running remote job from a dead one.
Without that, a job killed at 03:00 leaves its sentinel absent forever and the step
re-pauses, and re-pauses, and never tells anyone the run died (see "A pause must be able
to end in failure" in ``arf/specifications/step_tracker_specification.md``).

``resume_check`` runs the ``liveness_probe`` recorded by ``pause_step`` and reports the
branch that applies:

* no probe recorded → ``no_probe`` (exit 0)
* probe exits 0 → ``job_alive`` (exit 0)
* probe exits non-zero → ``job_dead`` (exit 3)
* probe times out → ``job_alive`` (exit 0), because a hung SSH is not evidence of death

A step that is not ``paused_waiting`` is an error (exit 1).
"""

import json
import sys
from pathlib import Path

import pytest

import arf.scripts.utils.resume_check as resume_check_module
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.task_builder import (
    build_step_tracker,
    build_task_folder,
    build_task_json,
)

TASK_ID: str = "t0001_test"
STEP_NUMBER: int = 5
PAUSE_COUNT: int = 3
MODULE_NAME: str = "arf.scripts.utils.resume_check"

# Schema keys are spelled out literally, never imported from the module under test: a
# fixture bound to the implementation's own constants stays green through a rename while
# production reads a field nothing writes.
STEP_FIELD: str = "step"
STATUS_FIELD: str = "status"
STARTED_AT_FIELD: str = "started_at"
COMPLETED_AT_FIELD: str = "completed_at"
LIVENESS_PROBE_FIELD: str = "liveness_probe"
PAUSE_COUNT_FIELD: str = "pause_count"
WATCHDOG_ACTIVE_FIELD: str = "watchdog_active"
RESUME_SENTINEL_FIELD: str = "resume_sentinel"
RESUME_AFTER_FIELD: str = "resume_after"

STATUS_PAUSED_WAITING: str = "paused_waiting"
STATUS_IN_PROGRESS: str = "in_progress"

DECISION_KEY: str = "decision"
PROBE_KEY: str = "probe"
RETURNCODE_KEY: str = "returncode"
PAUSE_COUNT_KEY: str = "pause_count"

DECISION_NO_PROBE: str = "no_probe"
DECISION_JOB_ALIVE: str = "job_alive"
DECISION_JOB_DEAD: str = "job_dead"

EXIT_OK: int = 0
EXIT_NOT_PAUSED: int = 1
EXIT_JOB_DEAD: int = 3

PROBE_ALIVE: str = "true"
PROBE_DEAD: str = "false"
PROBE_HANGS: str = "sleep 5"

_OMIT: object = object()


def _setup(*, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=repo_root,
        verificator_modules=[resume_check_module],
    )


def _paused_step(*, liveness_probe: object = _OMIT) -> dict[str, object]:
    step: dict[str, object] = {
        STEP_FIELD: STEP_NUMBER,
        "name": "implementation",
        "description": "Paused on a multi-hour training run.",
        STATUS_FIELD: STATUS_PAUSED_WAITING,
        STARTED_AT_FIELD: "2026-05-20T08:00:00Z",
        COMPLETED_AT_FIELD: None,
        "log_file": None,
        RESUME_SENTINEL_FIELD: "adapter at /mnt/cache/persist/runs/sft_v1/adapter",
        RESUME_AFTER_FIELD: "2026-05-20T12:00:00Z",
        WATCHDOG_ACTIVE_FIELD: True,
        PAUSE_COUNT_FIELD: PAUSE_COUNT,
    }
    if liveness_probe is not _OMIT:
        step[LIVENESS_PROBE_FIELD] = liveness_probe
    return step


def _in_progress_step() -> dict[str, object]:
    return {
        STEP_FIELD: STEP_NUMBER,
        "name": "implementation",
        "description": "Still being driven.",
        STATUS_FIELD: STATUS_IN_PROGRESS,
        STARTED_AT_FIELD: "2026-05-20T08:00:00Z",
        COMPLETED_AT_FIELD: None,
        "log_file": None,
    }


def _build_task(*, repo_root: Path, steps: list[dict[str, object]]) -> None:
    build_task_folder(repo_root=repo_root, task_id=TASK_ID)
    build_task_json(repo_root=repo_root, task_id=TASK_ID)
    build_step_tracker(repo_root=repo_root, task_id=TASK_ID, steps=steps)


def _run_cli(*, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", [MODULE_NAME, *args])
    try:
        outcome: object = resume_check_module.main()
    except SystemExit as exit_call:
        code: object = exit_call.code
        if code is None:
            return 0
        assert isinstance(code, int), f"the CLI exits with an int status; got {code!r}"
        return code
    if isinstance(outcome, int):
        return outcome
    return 0


def _read_payload(*, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured: str = capsys.readouterr().out
    lines: list[str] = [line for line in captured.splitlines() if len(line.strip()) > 0]
    assert len(lines) >= 1, f"resume_check must print a JSON decision; got {captured!r}"
    payload: object = json.loads(lines[-1])
    assert isinstance(payload, dict), "the decision payload is a JSON object"
    for key in [DECISION_KEY, PROBE_KEY, RETURNCODE_KEY, PAUSE_COUNT_KEY]:
        assert key in payload, f"the decision payload must always carry {key!r}; got {payload}"
    return payload


# ---------------------------------------------------------------------------
# No probe recorded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("liveness_probe", [_OMIT, None])
def test_no_probe_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    liveness_probe: object,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=[_paused_step(liveness_probe=liveness_probe)])

    exit_code: int = _run_cli(monkeypatch=monkeypatch, args=[TASK_ID, str(STEP_NUMBER)])

    assert exit_code == EXIT_OK
    payload: dict[str, object] = _read_payload(capsys=capsys)
    assert payload[DECISION_KEY] == DECISION_NO_PROBE
    assert payload[PROBE_KEY] is None
    assert payload[RETURNCODE_KEY] is None
    assert payload[PAUSE_COUNT_KEY] == PAUSE_COUNT


# ---------------------------------------------------------------------------
# Probe outcomes
# ---------------------------------------------------------------------------


def test_probe_exit_zero_is_job_alive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=[_paused_step(liveness_probe=PROBE_ALIVE)])

    exit_code: int = _run_cli(monkeypatch=monkeypatch, args=[TASK_ID, str(STEP_NUMBER)])

    assert exit_code == EXIT_OK
    payload: dict[str, object] = _read_payload(capsys=capsys)
    assert payload[DECISION_KEY] == DECISION_JOB_ALIVE
    assert payload[PROBE_KEY] == PROBE_ALIVE
    assert payload[RETURNCODE_KEY] == 0


def test_probe_exit_nonzero_is_job_dead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=[_paused_step(liveness_probe=PROBE_DEAD)])

    exit_code: int = _run_cli(monkeypatch=monkeypatch, args=[TASK_ID, str(STEP_NUMBER)])

    assert exit_code == EXIT_JOB_DEAD, "a dead job must be distinguishable by exit code"
    payload: dict[str, object] = _read_payload(capsys=capsys)
    assert payload[DECISION_KEY] == DECISION_JOB_DEAD
    assert payload[PROBE_KEY] == PROBE_DEAD
    returncode: object = payload[RETURNCODE_KEY]
    assert isinstance(returncode, int), "a probe that ran reports its exit code"
    assert returncode != 0


def test_probe_timeout_is_treated_as_alive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A hung SSH is not evidence the job died; calling it dead would abort a healthy run.
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=[_paused_step(liveness_probe=PROBE_HANGS)])

    exit_code: int = _run_cli(
        monkeypatch=monkeypatch,
        args=[TASK_ID, str(STEP_NUMBER), "--timeout-seconds", "1"],
    )

    assert exit_code == EXIT_OK
    payload: dict[str, object] = _read_payload(capsys=capsys)
    assert payload[DECISION_KEY] == DECISION_JOB_ALIVE
    assert payload[PROBE_KEY] == PROBE_HANGS


# ---------------------------------------------------------------------------
# Not a paused step
# ---------------------------------------------------------------------------


def test_step_not_paused_waiting_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=[_in_progress_step()])

    exit_code: int = _run_cli(monkeypatch=monkeypatch, args=[TASK_ID, str(STEP_NUMBER)])

    assert exit_code == EXIT_NOT_PAUSED
    assert len(capsys.readouterr().err.strip()) > 0, "the reason must reach stderr"
