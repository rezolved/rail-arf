"""Tests for the verify_step_liveness verificator (ST-E007 / ST-W005 / ST-W006).

The verificator inspects ``step_tracker.json`` for each task and, for steps
with ``status == "in_progress"``, classifies them into one of:

* ``ST-E007`` — stale heartbeat AND a live VM (machine_log entry with
  ``actual_status == "running"``) is present.
* ``ST-W005`` — stale heartbeat AND no live VM.
* ``ST-W006`` — fresh heartbeat but elapsed wall-clock exceeds the expected
  duration scaled by ``slow_factor``.

Steps that are completed, or that lack the v2 liveness fields, are skipped
without diagnostics. When ``task_id`` is None the verificator scans every task.

The implementation does not yet exist; these tests fail with an import error.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import arf.scripts.verificators.verify_step_liveness as verify_step_liveness_module
from arf.scripts.verificators.common import paths
from arf.scripts.verificators.common.types import VerificationResult
from arf.scripts.verificators.verify_step_liveness import verify_step_liveness
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.task_builder import (
    build_step_tracker,
    build_task_folder,
    build_task_json,
)

TASK_ID: str = "t0001_test"
OTHER_TASK_ID: str = "t0002_other"
STEP_NUMBER: int = 5
COMPLETED_STEP_NUMBER: int = 1
HEARTBEAT_INTERVAL_SECONDS: int = 60
STALENESS_MULTIPLIER: int = 3
SETUP_MACHINES_STEP_DIR_NAME: str = "008_setup-machines"
MACHINE_LOG_FILENAME: str = "machine_log.json"
RUNNING_STATUS: str = "running"
DESTROYED_STATUS: str = "destroyed"

STATUS_FIELD: str = "status"
STATUS_PENDING: str = "pending"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_COMPLETED: str = "completed"

STARTED_AT_FIELD: str = "started_at"
COMPLETED_AT_FIELD: str = "completed_at"
LAST_HEARTBEAT_AT_FIELD: str = "last_heartbeat_at"
HEARTBEAT_INTERVAL_FIELD: str = "heartbeat_interval_seconds"
EXPECTED_COMPLETION_FIELD: str = "expected_completion_at"
STEP_FIELD: str = "step"

CODE_ST_E007: str = "ST-E007"
CODE_ST_W005: str = "ST-W005"
CODE_ST_W006: str = "ST-W006"


def _setup(*, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=repo_root,
        verificator_modules=[verify_step_liveness_module],
    )


def _codes(result: VerificationResult) -> list[str]:
    return [d.code.text for d in result.diagnostics]


def _all_codes(results: list[VerificationResult]) -> list[str]:
    out: list[str] = []
    for result in results:
        out.extend(_codes(result=result))
    return out


def _iso_z(*, dt: datetime) -> str:
    aware: datetime = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_progress_step(
    *,
    step_number: int = STEP_NUMBER,
    started_at: str,
    last_heartbeat_at: str | None,
    heartbeat_interval_seconds: int | None,
    expected_completion_at: str | None,
) -> dict[str, object]:
    step: dict[str, object] = {
        STEP_FIELD: step_number,
        "name": "implementation",
        "description": "Run implementation.",
        STATUS_FIELD: STATUS_IN_PROGRESS,
        STARTED_AT_FIELD: started_at,
        COMPLETED_AT_FIELD: None,
        "log_file": None,
    }
    if last_heartbeat_at is not None:
        step[LAST_HEARTBEAT_AT_FIELD] = last_heartbeat_at
    if heartbeat_interval_seconds is not None:
        step[HEARTBEAT_INTERVAL_FIELD] = heartbeat_interval_seconds
    if expected_completion_at is not None:
        step[EXPECTED_COMPLETION_FIELD] = expected_completion_at
    return step


def _completed_step(
    *,
    step_number: int = COMPLETED_STEP_NUMBER,
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    return {
        STEP_FIELD: step_number,
        "name": "create-branch",
        "description": "Create branch.",
        STATUS_FIELD: STATUS_COMPLETED,
        STARTED_AT_FIELD: started_at,
        COMPLETED_AT_FIELD: completed_at,
        "log_file": "logs/steps/001_create-branch/",
    }


def _build_task(
    *,
    repo_root: Path,
    task_id: str,
    steps: list[dict[str, object]],
) -> None:
    build_task_folder(repo_root=repo_root, task_id=task_id)
    build_task_json(repo_root=repo_root, task_id=task_id)
    build_step_tracker(repo_root=repo_root, task_id=task_id, steps=steps)


def _write_machine_log(
    *,
    task_id: str,
    actual_status: str,
) -> Path:
    step_dir: Path = paths.step_logs_dir(task_id=task_id) / SETUP_MACHINES_STEP_DIR_NAME
    step_dir.mkdir(parents=True, exist_ok=True)
    log_path: Path = step_dir / MACHINE_LOG_FILENAME
    entry: dict[str, object] = {
        "provider": "vast.ai",
        "instance_id": "12345678",
        "actual_status": actual_status,
        "created_at": "2026-05-20T08:00:00Z",
        "destroyed_at": (None if actual_status == RUNNING_STATUS else "2026-05-20T09:00:00Z"),
    }
    log_path.write_text(json.dumps([entry]), encoding="utf-8")
    return log_path


def _invoke(
    *,
    task_id: str | None = TASK_ID,
    now: datetime,
    slow_factor: float = 2.0,
) -> VerificationResult | list[VerificationResult]:
    return verify_step_liveness(
        task_id=task_id,
        now=now,
        slow_factor=slow_factor,
    )


def _as_results_list(
    result: VerificationResult | list[VerificationResult],
) -> list[VerificationResult]:
    if isinstance(result, list):
        return result
    return [result]


# ---------------------------------------------------------------------------
# ST-E007: stale heartbeat AND live VM → error
# ---------------------------------------------------------------------------


def test_st_e007_stale_heartbeat_with_live_vm_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    # Heartbeat 1 hour ago, interval 60s -> age 3600 > 3*60 = 180.
    stale_heartbeat: datetime = now - timedelta(hours=1)
    started_at: datetime = now - timedelta(hours=2)
    expected_completion: datetime = now + timedelta(hours=1)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=started_at),
                last_heartbeat_at=_iso_z(dt=stale_heartbeat),
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                expected_completion_at=_iso_z(dt=expected_completion),
            ),
        ],
    )
    _write_machine_log(task_id=TASK_ID, actual_status=RUNNING_STATUS)

    results: list[VerificationResult] = _as_results_list(
        _invoke(task_id=TASK_ID, now=now),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_E007 in codes, f"expected ST-E007 in {codes}"


# ---------------------------------------------------------------------------
# ST-W005: stale heartbeat AND no machine_log → warning
# ---------------------------------------------------------------------------


def test_st_w005_stale_heartbeat_no_vm_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    stale_heartbeat: datetime = now - timedelta(hours=1)
    started_at: datetime = now - timedelta(hours=2)
    expected_completion: datetime = now + timedelta(hours=1)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=started_at),
                last_heartbeat_at=_iso_z(dt=stale_heartbeat),
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                expected_completion_at=_iso_z(dt=expected_completion),
            ),
        ],
    )
    # Deliberately NO machine_log.json.

    results: list[VerificationResult] = _as_results_list(
        _invoke(task_id=TASK_ID, now=now),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_W005 in codes, f"expected ST-W005 in {codes}"
    assert CODE_ST_E007 not in codes, f"ST-E007 must not fire without a live VM; got {codes}"


# ---------------------------------------------------------------------------
# ST-W005: stale heartbeat AND destroyed VM → warning (no error)
# ---------------------------------------------------------------------------


def test_st_w005_stale_heartbeat_destroyed_vm_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    stale_heartbeat: datetime = now - timedelta(hours=1)
    started_at: datetime = now - timedelta(hours=2)
    expected_completion: datetime = now + timedelta(hours=1)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=started_at),
                last_heartbeat_at=_iso_z(dt=stale_heartbeat),
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                expected_completion_at=_iso_z(dt=expected_completion),
            ),
        ],
    )
    _write_machine_log(task_id=TASK_ID, actual_status=DESTROYED_STATUS)

    results: list[VerificationResult] = _as_results_list(
        _invoke(task_id=TASK_ID, now=now),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_W005 in codes, f"expected ST-W005 in {codes}"
    assert CODE_ST_E007 not in codes, f"ST-E007 must not fire when VM is destroyed; got {codes}"


# ---------------------------------------------------------------------------
# ST-W006: fresh heartbeat but elapsed > expected × slow_factor
# ---------------------------------------------------------------------------


def test_st_w006_alive_but_over_expected_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    # Expected duration = 1 hour. Slow factor 2.0 means we tolerate up to
    # expected_completion_at + 1 hour. Place ``now`` well past that boundary.
    started_at: datetime = now - timedelta(hours=5)
    expected_completion: datetime = started_at + timedelta(hours=1)
    fresh_heartbeat: datetime = now - timedelta(seconds=10)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=started_at),
                last_heartbeat_at=_iso_z(dt=fresh_heartbeat),
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                expected_completion_at=_iso_z(dt=expected_completion),
            ),
        ],
    )

    results: list[VerificationResult] = _as_results_list(
        _invoke(task_id=TASK_ID, now=now, slow_factor=2.0),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_W006 in codes, f"expected ST-W006 in {codes}"
    assert CODE_ST_E007 not in codes, f"ST-E007 must not fire with fresh heartbeat; got {codes}"


# ---------------------------------------------------------------------------
# Fresh heartbeat under expected: no diagnostics
# ---------------------------------------------------------------------------


def test_fresh_heartbeat_under_expected_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    started_at: datetime = now - timedelta(minutes=10)
    expected_completion: datetime = now + timedelta(hours=1)
    fresh_heartbeat: datetime = now - timedelta(seconds=5)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=started_at),
                last_heartbeat_at=_iso_z(dt=fresh_heartbeat),
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                expected_completion_at=_iso_z(dt=expected_completion),
            ),
        ],
    )

    results: list[VerificationResult] = _as_results_list(
        _invoke(task_id=TASK_ID, now=now),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_E007 not in codes
    assert CODE_ST_W005 not in codes
    assert CODE_ST_W006 not in codes


# ---------------------------------------------------------------------------
# Completed step is never flagged regardless of timestamps
# ---------------------------------------------------------------------------


def test_completed_step_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    started_at: datetime = now - timedelta(days=30)
    completed_at: datetime = now - timedelta(days=29)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _completed_step(
                started_at=_iso_z(dt=started_at),
                completed_at=_iso_z(dt=completed_at),
            ),
        ],
    )

    results: list[VerificationResult] = _as_results_list(
        _invoke(task_id=TASK_ID, now=now),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_E007 not in codes
    assert CODE_ST_W005 not in codes
    assert CODE_ST_W006 not in codes


# ---------------------------------------------------------------------------
# Backward-compat: missing v2 fields → silent skip
# ---------------------------------------------------------------------------


def test_missing_heartbeat_fields_backward_compat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    started_at: datetime = now - timedelta(hours=10)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=started_at),
                last_heartbeat_at=None,
                heartbeat_interval_seconds=None,
                expected_completion_at=None,
            ),
        ],
    )

    results: list[VerificationResult] = _as_results_list(
        _invoke(task_id=TASK_ID, now=now),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_E007 not in codes
    assert CODE_ST_W005 not in codes
    assert CODE_ST_W006 not in codes


# ---------------------------------------------------------------------------
# task_id=None: scan all tasks, only flag the stale one
# ---------------------------------------------------------------------------


def test_scan_all_tasks_when_task_id_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    stale_heartbeat: datetime = now - timedelta(hours=1)
    started_at_stale: datetime = now - timedelta(hours=2)
    expected_completion_stale: datetime = now + timedelta(hours=1)

    # Stale task — must flag.
    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=started_at_stale),
                last_heartbeat_at=_iso_z(dt=stale_heartbeat),
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                expected_completion_at=_iso_z(dt=expected_completion_stale),
            ),
        ],
    )

    # Healthy task — must NOT flag.
    started_at_fresh: datetime = now - timedelta(minutes=5)
    expected_completion_fresh: datetime = now + timedelta(hours=1)
    fresh_heartbeat: datetime = now - timedelta(seconds=2)
    _build_task(
        repo_root=tmp_path,
        task_id=OTHER_TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=started_at_fresh),
                last_heartbeat_at=_iso_z(dt=fresh_heartbeat),
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                expected_completion_at=_iso_z(dt=expected_completion_fresh),
            ),
        ],
    )

    results: list[VerificationResult] = _as_results_list(
        _invoke(task_id=None, now=now),
    )
    codes: list[str] = _all_codes(results=results)
    assert (CODE_ST_E007 in codes) or (CODE_ST_W005 in codes), (
        f"stale task must produce a stale-heartbeat diagnostic; got {codes}"
    )

    # The healthy task must not generate diagnostics. Identify diagnostics
    # tied to OTHER_TASK_ID by inspecting the file_path on each diagnostic.
    other_task_dir: Path = paths.task_dir(task_id=OTHER_TASK_ID)
    other_codes: list[str] = []
    for result in results:
        for diagnostic in result.diagnostics:
            try:
                diagnostic.file_path.relative_to(other_task_dir)
            except ValueError:
                continue
            other_codes.append(diagnostic.code.text)
    assert other_codes == [], f"healthy task must produce no diagnostics; got {other_codes}"


# ---------------------------------------------------------------------------
# ST-E008: paused_waiting step without an active watchdog → error;
# with an active watchdog → safe (no diagnostic, not ghosted)
# ---------------------------------------------------------------------------

STATUS_PAUSED_WAITING: str = "paused_waiting"
WATCHDOG_ACTIVE_FIELD: str = "watchdog_active"
RESUME_SENTINEL_FIELD: str = "resume_sentinel"
RESUME_AFTER_FIELD: str = "resume_after"
PAUSED_STEP_NUMBER: int = 7
CODE_ST_E008: str = "ST-E008"


def _paused_step(*, watchdog_active: object) -> dict[str, object]:
    # A paused step's heartbeat is intentionally stale (no one is driving it). With a watchdog it
    # must NOT be ghost-flagged; without one it must be flagged ST-E008.
    return {
        STEP_FIELD: PAUSED_STEP_NUMBER,
        "name": "implementation",
        "description": "Paused on a long benchmark wait.",
        STATUS_FIELD: STATUS_PAUSED_WAITING,
        STARTED_AT_FIELD: "2026-05-20T08:00:00Z",
        COMPLETED_AT_FIELD: None,
        LAST_HEARTBEAT_AT_FIELD: "2026-05-20T08:05:00Z",
        HEARTBEAT_INTERVAL_FIELD: HEARTBEAT_INTERVAL_SECONDS,
        EXPECTED_COMPLETION_FIELD: "2026-05-20T09:00:00Z",
        "current_owner": None,
        RESUME_SENTINEL_FIELD: "bench output ~/done.json on vast 12345",
        RESUME_AFTER_FIELD: "2026-05-20T08:40:00Z",
        WATCHDOG_ACTIVE_FIELD: watchdog_active,
    }


def test_paused_with_watchdog_is_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    # Hours after the stale heartbeat — a normal in_progress step would be ST-W005/E007 here.
    now: datetime = datetime(2026, 5, 20, 14, 0, 0, tzinfo=UTC)
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[_paused_step(watchdog_active=True)])
    _write_machine_log(task_id=TASK_ID, actual_status=RUNNING_STATUS)

    result: VerificationResult = _as_results_list(_invoke(now=now))[0]
    assert _codes(result) == [], "paused_waiting with an active watchdog must not be flagged"
    assert result.passed


def test_paused_without_watchdog_errors_st_e008(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 14, 0, 0, tzinfo=UTC)
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[_paused_step(watchdog_active=False)])

    result: VerificationResult = _as_results_list(_invoke(now=now))[0]
    assert CODE_ST_E008 in _codes(result), "paused without a watchdog must raise ST-E008"
    assert not result.passed
