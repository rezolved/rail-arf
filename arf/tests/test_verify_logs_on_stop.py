"""Tests for the ``Stop`` hook in ``arf.scripts.hooks.verify_logs_on_stop``.

The hook is the only component that runs on every attempt to end a turn, which makes it
the one place where "do not walk away from a running step" can be enforced rather than
requested. ``execute-task`` has stated that rule in prose since v26 and a step-executor
violated it anyway, leaving an H100 idle for ~20 hours (see Lesson 8 in ``LESSONS.md``).

The hook blocks (exit 2, message on stderr) when a live task still has an ``in_progress``
step. It allows the stop when:

* the step was handed over properly (``paused_waiting``), or
* it already blocked once this turn (``stop_hook_active``), or
* the ``in_progress`` step belongs to a task that is no longer live — stale data in a
  completed task must never brick every future stop.

Checking ``in_progress`` rather than a stale heartbeat is deliberate: at the moment a turn
ends the heartbeat is still fresh, and there is no later turn whose stop could be caught.
"""

import io
import json
from pathlib import Path

import pytest

import arf.scripts.hooks.verify_logs_on_stop as hook_module
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.task_builder import (
    build_step_tracker,
    build_task_folder,
    build_task_json,
)

TASK_ID: str = "t0001_test"
STEP_NUMBER: int = 7
STEP_NAME: str = "implementation"
BLOCK_EXIT_CODE: int = 2

# Schema keys are spelled out literally, never imported from the module under test: a
# fixture bound to the implementation's own constants stays green through a rename while
# production reads a field nothing writes.
STEP_FIELD: str = "step"
NAME_FIELD: str = "name"
DESCRIPTION_FIELD: str = "description"
STATUS_FIELD: str = "status"
STOP_HOOK_ACTIVE_FIELD: str = "stop_hook_active"

STATUS_IN_PROGRESS: str = "in_progress"
STATUS_PAUSED_WAITING: str = "paused_waiting"
STATUS_COMPLETED: str = "completed"


def _build_step(*, status: str) -> dict[str, object]:
    return {
        STEP_FIELD: STEP_NUMBER,
        NAME_FIELD: STEP_NAME,
        DESCRIPTION_FIELD: "Run the training job on the remote machine.",
        STATUS_FIELD: status,
    }


def _prepare_repo(
    *,
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    step_status: str,
    task_status: str,
) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=repo_root,
        verificator_modules=[hook_module],
    )
    build_task_folder(repo_root=repo_root, task_id=TASK_ID)
    build_task_json(repo_root=repo_root, task_id=TASK_ID, status=task_status)
    build_step_tracker(
        repo_root=repo_root,
        task_id=TASK_ID,
        steps=[_build_step(status=step_status)],
    )


def _set_hook_stdin(*, monkeypatch: pytest.MonkeyPatch, stop_hook_active: bool) -> None:
    payload: str = json.dumps({STOP_HOOK_ACTIVE_FIELD: stop_hook_active})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))


def test_blocks_stop_when_a_live_task_has_an_in_progress_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_repo(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        step_status=STATUS_IN_PROGRESS,
        task_status=STATUS_IN_PROGRESS,
    )
    _set_hook_stdin(monkeypatch=monkeypatch, stop_hook_active=False)

    with pytest.raises(SystemExit) as exit_info:
        hook_module.main()

    assert exit_info.value.code == BLOCK_EXIT_CODE, "an unattended step blocks the stop"
    stderr: str = capsys.readouterr().err
    assert TASK_ID in stderr, "the block message names the task"
    assert str(STEP_NUMBER) in stderr, "the block message names the step number"
    assert "pause" in stderr.lower(), "the block message points at the handover path"
    assert "do not touch it" in stderr, (
        "the block message warns off a session that does not own the step — the check is "
        "repo-wide, so an uninvolved turn can be blocked by another session's live step, "
        "and 'drive the step to a terminal state' taken literally there is destructive"
    )


def test_allows_stop_when_the_step_was_paused_properly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_repo(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        step_status=STATUS_PAUSED_WAITING,
        task_status=STATUS_IN_PROGRESS,
    )
    _set_hook_stdin(monkeypatch=monkeypatch, stop_hook_active=False)

    with pytest.raises(SystemExit) as exit_info:
        hook_module.main()

    assert exit_info.value.code == 0, "a properly handed-over step does not block the stop"


def test_allows_stop_when_the_hook_already_blocked_this_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_repo(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        step_status=STATUS_IN_PROGRESS,
        task_status=STATUS_IN_PROGRESS,
    )
    _set_hook_stdin(monkeypatch=monkeypatch, stop_hook_active=True)

    with pytest.raises(SystemExit) as exit_info:
        hook_module.main()

    assert exit_info.value.code == 0, "stop_hook_active prevents an unstoppable turn"


def test_allows_stop_when_the_in_progress_step_belongs_to_a_finished_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_repo(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        step_status=STATUS_IN_PROGRESS,
        task_status=STATUS_COMPLETED,
    )
    _set_hook_stdin(monkeypatch=monkeypatch, stop_hook_active=False)

    with pytest.raises(SystemExit) as exit_info:
        hook_module.main()

    assert exit_info.value.code == 0, "stale data in a completed task never blocks a stop"


def test_allows_stop_when_no_tasks_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        verificator_modules=[hook_module],
    )
    _set_hook_stdin(monkeypatch=monkeypatch, stop_hook_active=False)

    with pytest.raises(SystemExit) as exit_info:
        hook_module.main()

    assert exit_info.value.code == 0, "an empty repo does not block a stop"


def test_missing_hook_payload_is_treated_as_a_first_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_repo(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        step_status=STATUS_IN_PROGRESS,
        task_status=STATUS_IN_PROGRESS,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as exit_info:
        hook_module.main()

    assert exit_info.value.code == BLOCK_EXIT_CODE, "an unreadable payload still blocks"
