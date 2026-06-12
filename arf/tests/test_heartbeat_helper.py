"""Tests for the heartbeat helper module (arf.scripts.utils.heartbeat).

Tests describe the contract for the step_tracker.json heartbeat library:

* ``start_step`` initializes liveness fields on an existing tracker step.
* ``write_heartbeat`` refreshes ``last_heartbeat_at`` and ``current_owner``.
* ``complete_step`` finalizes the step and computes ``actual_duration_seconds``.
* Missing step number raises ``ValueError``; missing tracker raises ``FileNotFoundError``.

The implementation does not yet exist; these tests fail with an import error.
"""

import json
import re
from pathlib import Path

import pytest

import arf.scripts.verificators.verify_step as verify_step_module
from arf.scripts.utils.heartbeat import (
    complete_step,
    pause_step,
    start_step,
    write_heartbeat,
)
from arf.scripts.verificators.common import paths
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.task_builder import (
    build_step_tracker,
    build_task_folder,
)

TASK_ID: str = "t0001_test"
STEP_NUMBER: int = 3
OTHER_STEP_NUMBER: int = 4
MISSING_STEP_NUMBER: int = 99
CURRENT_OWNER: str = "implementation-subagent-pid42"
OTHER_OWNER: str = "implementation-subagent-pid99"
HEARTBEAT_INTERVAL_SECONDS: int = 60
EXPECTED_COMPLETION_AT: str = "2026-05-20T12:00:00Z"

ISO_8601_UTC_PATTERN: re.Pattern[str] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$",
)

STATUS_FIELD: str = "status"
STATUS_PENDING: str = "pending"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_COMPLETED: str = "completed"

STARTED_AT_FIELD: str = "started_at"
COMPLETED_AT_FIELD: str = "completed_at"
LAST_HEARTBEAT_AT_FIELD: str = "last_heartbeat_at"
CURRENT_OWNER_FIELD: str = "current_owner"
HEARTBEAT_INTERVAL_FIELD: str = "heartbeat_interval_seconds"
EXPECTED_COMPLETION_FIELD: str = "expected_completion_at"
ACTUAL_DURATION_FIELD: str = "actual_duration_seconds"
STEPS_FIELD: str = "steps"
STEP_FIELD: str = "step"


def _setup(*, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=repo_root,
        verificator_modules=[verify_step_module],
    )


def _default_steps() -> list[dict[str, object]]:
    return [
        {
            STEP_FIELD: 1,
            "name": "create-branch",
            "description": "Create branch.",
            STATUS_FIELD: STATUS_COMPLETED,
            STARTED_AT_FIELD: "2026-04-01T00:00:00Z",
            COMPLETED_AT_FIELD: "2026-04-01T00:00:05Z",
            "log_file": "logs/steps/001_create-branch/",
        },
        {
            STEP_FIELD: 2,
            "name": "check-deps",
            "description": "Check deps.",
            STATUS_FIELD: STATUS_COMPLETED,
            STARTED_AT_FIELD: "2026-04-01T00:01:00Z",
            COMPLETED_AT_FIELD: "2026-04-01T00:01:10Z",
            "log_file": "logs/steps/002_check-deps/",
        },
        {
            STEP_FIELD: STEP_NUMBER,
            "name": "implementation",
            "description": "Run implementation.",
            STATUS_FIELD: STATUS_PENDING,
            STARTED_AT_FIELD: None,
            COMPLETED_AT_FIELD: None,
            "log_file": None,
        },
        {
            STEP_FIELD: OTHER_STEP_NUMBER,
            "name": "teardown",
            "description": "Teardown.",
            STATUS_FIELD: STATUS_PENDING,
            STARTED_AT_FIELD: None,
            COMPLETED_AT_FIELD: None,
            "log_file": None,
        },
    ]


def _read_tracker(*, task_id: str) -> dict[str, object]:
    tracker_path: Path = paths.step_tracker_path(task_id=task_id)
    data: object = json.loads(tracker_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "tracker root is a JSON object"
    return data


def _get_step(
    *,
    tracker: dict[str, object],
    step_number: int,
) -> dict[str, object]:
    steps: list[object] = tracker[STEPS_FIELD]  # type: ignore[assignment]
    for step in steps:
        assert isinstance(step, dict), "step entry is a dict"
        if step[STEP_FIELD] == step_number:
            return step
    raise AssertionError(f"step {step_number} not found in tracker")


def _build_tracker_with_default_steps(
    *,
    repo_root: Path,
    task_id: str = TASK_ID,
) -> None:
    build_task_folder(repo_root=repo_root, task_id=task_id)
    build_step_tracker(
        repo_root=repo_root,
        task_id=task_id,
        steps=_default_steps(),
    )


# ---------------------------------------------------------------------------
# write_heartbeat updates timestamp + owner with ISO 8601 Z format
# ---------------------------------------------------------------------------


def test_write_heartbeat_updates_timestamp_and_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_tracker_with_default_steps(repo_root=tmp_path)

    start_step(
        task_id=TASK_ID,
        step_number=STEP_NUMBER,
        current_owner=CURRENT_OWNER,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        expected_completion_at=EXPECTED_COMPLETION_AT,
    )

    write_heartbeat(
        task_id=TASK_ID,
        step_number=STEP_NUMBER,
        current_owner=OTHER_OWNER,
    )

    tracker: dict[str, object] = _read_tracker(task_id=TASK_ID)
    step: dict[str, object] = _get_step(tracker=tracker, step_number=STEP_NUMBER)
    last_heartbeat: object = step[LAST_HEARTBEAT_AT_FIELD]
    assert isinstance(last_heartbeat, str), "last_heartbeat_at is a string"
    assert ISO_8601_UTC_PATTERN.match(last_heartbeat) is not None, (
        f"last_heartbeat_at must match ISO 8601 Z; got {last_heartbeat!r}"
    )
    assert step[CURRENT_OWNER_FIELD] == OTHER_OWNER


# ---------------------------------------------------------------------------
# start_step initializes all liveness fields
# ---------------------------------------------------------------------------


def test_start_step_initializes_all_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_tracker_with_default_steps(repo_root=tmp_path)

    start_step(
        task_id=TASK_ID,
        step_number=STEP_NUMBER,
        current_owner=CURRENT_OWNER,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        expected_completion_at=EXPECTED_COMPLETION_AT,
    )

    tracker: dict[str, object] = _read_tracker(task_id=TASK_ID)
    step: dict[str, object] = _get_step(tracker=tracker, step_number=STEP_NUMBER)

    assert step[STATUS_FIELD] == STATUS_IN_PROGRESS

    started_at: object = step[STARTED_AT_FIELD]
    assert isinstance(started_at, str), "started_at is a string"
    assert ISO_8601_UTC_PATTERN.match(started_at) is not None, (
        f"started_at must match ISO 8601 Z; got {started_at!r}"
    )

    last_heartbeat: object = step[LAST_HEARTBEAT_AT_FIELD]
    assert isinstance(last_heartbeat, str), "last_heartbeat_at is a string"
    assert ISO_8601_UTC_PATTERN.match(last_heartbeat) is not None, (
        f"last_heartbeat_at must match ISO 8601 Z; got {last_heartbeat!r}"
    )

    assert step[CURRENT_OWNER_FIELD] == CURRENT_OWNER
    assert step[HEARTBEAT_INTERVAL_FIELD] == HEARTBEAT_INTERVAL_SECONDS
    assert step[EXPECTED_COMPLETION_FIELD] == EXPECTED_COMPLETION_AT


# ---------------------------------------------------------------------------
# complete_step finalizes status, computes duration, clears owner
# ---------------------------------------------------------------------------


def test_complete_step_clears_owner_sets_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_tracker_with_default_steps(repo_root=tmp_path)

    start_step(
        task_id=TASK_ID,
        step_number=STEP_NUMBER,
        current_owner=CURRENT_OWNER,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        expected_completion_at=EXPECTED_COMPLETION_AT,
    )

    complete_step(task_id=TASK_ID, step_number=STEP_NUMBER)

    tracker: dict[str, object] = _read_tracker(task_id=TASK_ID)
    step: dict[str, object] = _get_step(tracker=tracker, step_number=STEP_NUMBER)

    assert step[STATUS_FIELD] == STATUS_COMPLETED

    completed_at: object = step[COMPLETED_AT_FIELD]
    assert isinstance(completed_at, str), "completed_at is a string"
    assert ISO_8601_UTC_PATTERN.match(completed_at) is not None, (
        f"completed_at must match ISO 8601 Z; got {completed_at!r}"
    )

    duration: object = step[ACTUAL_DURATION_FIELD]
    assert isinstance(duration, int), "actual_duration_seconds is an int"
    assert duration >= 0, "actual_duration_seconds is non-negative"

    assert step[CURRENT_OWNER_FIELD] is None


# ---------------------------------------------------------------------------
# write_heartbeat to non-existent step raises ValueError
# ---------------------------------------------------------------------------


def test_write_heartbeat_missing_step_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_tracker_with_default_steps(repo_root=tmp_path)

    with pytest.raises(ValueError):
        write_heartbeat(
            task_id=TASK_ID,
            step_number=MISSING_STEP_NUMBER,
            current_owner=CURRENT_OWNER,
        )


# ---------------------------------------------------------------------------
# write_heartbeat with missing tracker raises FileNotFoundError
# ---------------------------------------------------------------------------


def test_write_heartbeat_missing_tracker_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    # Build the task folder but deliberately do NOT create step_tracker.json.
    build_task_folder(repo_root=tmp_path, task_id=TASK_ID)

    with pytest.raises(FileNotFoundError):
        write_heartbeat(
            task_id=TASK_ID,
            step_number=STEP_NUMBER,
            current_owner=CURRENT_OWNER,
        )


# ---------------------------------------------------------------------------
# pause_step transitions to paused_waiting (resume-from-files), requires watchdog
# ---------------------------------------------------------------------------

STATUS_PAUSED_WAITING: str = "paused_waiting"
RESUME_SENTINEL_FIELD: str = "resume_sentinel"
PAUSED_AT_FIELD: str = "paused_at"
RESUME_AFTER_FIELD: str = "resume_after"
WATCHDOG_ACTIVE_FIELD: str = "watchdog_active"
RESUME_SENTINEL: str = "benchmark output ~/bench_done.json on vast instance 12345"
RESUME_AFTER: str = "2026-05-20T13:00:00Z"


def test_pause_step_sets_paused_waiting_and_clears_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_tracker_with_default_steps(repo_root=tmp_path)

    start_step(
        task_id=TASK_ID,
        step_number=STEP_NUMBER,
        current_owner=CURRENT_OWNER,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        expected_completion_at=EXPECTED_COMPLETION_AT,
    )
    pause_step(
        task_id=TASK_ID,
        step_number=STEP_NUMBER,
        resume_sentinel=RESUME_SENTINEL,
        resume_after=RESUME_AFTER,
        watchdog_active=True,
    )

    tracker: dict[str, object] = _read_tracker(task_id=TASK_ID)
    step: dict[str, object] = _get_step(tracker=tracker, step_number=STEP_NUMBER)
    assert step[STATUS_FIELD] == STATUS_PAUSED_WAITING
    assert step[CURRENT_OWNER_FIELD] is None
    assert step[RESUME_SENTINEL_FIELD] == RESUME_SENTINEL
    assert step[RESUME_AFTER_FIELD] == RESUME_AFTER
    assert step[WATCHDOG_ACTIVE_FIELD] is True
    paused_at: object = step[PAUSED_AT_FIELD]
    assert isinstance(paused_at, str)
    assert ISO_8601_UTC_PATTERN.match(paused_at) is not None


def test_pause_step_without_watchdog_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_tracker_with_default_steps(repo_root=tmp_path)
    start_step(
        task_id=TASK_ID,
        step_number=STEP_NUMBER,
        current_owner=CURRENT_OWNER,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        expected_completion_at=EXPECTED_COMPLETION_AT,
    )
    with pytest.raises(AssertionError, match="watchdog"):
        pause_step(
            task_id=TASK_ID,
            step_number=STEP_NUMBER,
            resume_sentinel=RESUME_SENTINEL,
            resume_after=RESUME_AFTER,
            watchdog_active=False,
        )
    # The step must remain in_progress, not half-transitioned.
    tracker: dict[str, object] = _read_tracker(task_id=TASK_ID)
    step: dict[str, object] = _get_step(tracker=tracker, step_number=STEP_NUMBER)
    assert step[STATUS_FIELD] == STATUS_IN_PROGRESS
