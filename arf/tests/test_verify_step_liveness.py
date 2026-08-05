"""Tests for the verify_step_liveness verificator.

The verificator inspects ``step_tracker.json`` for each task and, for steps
with ``status == "in_progress"``, classifies them into one of:

* ``ST-E007`` — stale heartbeat AND a live VM is provisioned for the task.
* ``ST-W005`` — stale heartbeat AND no live VM.
* ``ST-W006`` — fresh heartbeat but elapsed wall-clock exceeds the expected
  duration scaled by ``slow_factor``.
* ``ST-E009`` — on a tracker that declares ``spec_version``, an ``in_progress``
  step whose ``last_heartbeat_at`` / ``heartbeat_interval_seconds`` /
  ``expected_completion_at`` is missing or of the wrong JSON type.

Independently of step state, a task with a live VM whose ``machine_log.json``
entry does not record ``watchdog_active: true`` is flagged ``ST-W008``.

Per ``arf/specifications/step_tracker_specification.md`` "Live VM Detection", a
machine is live when ``instance_id`` is a non-empty string AND ``destroyed_at``
is null or absent. There is no ``actual_status`` field in the machine-log
schema — it is a Vast.ai provider-API field that is never persisted — so no
fixture here writes one.

Steps that are completed, or that lack the liveness fields on a v1 tracker (no
``spec_version``), are skipped without diagnostics. When ``task_id`` is None the
verificator scans every task.
"""

import json
import sys
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
SETUP_MACHINES_STEP_DIR_NAME: str = "008_setup-machines"
MACHINE_LOG_FILENAME: str = "machine_log.json"

MACHINE_SPEC_VERSION: str = "6"
MACHINE_PROVIDER: str = "azure_ml"
MACHINE_INSTANCE_ID: str = "arf-NC80-weu-v1"
MACHINE_CREATED_AT: str = "2026-05-20T08:00:00Z"
MACHINE_READY_AT: str = "2026-05-20T08:06:40Z"
MACHINE_DESTROYED_AT: str = "2026-05-20T09:00:00Z"
WATCHDOG_IDLE_TIMEOUT_SECONDS: int = 1800

TRACKER_SPEC_VERSION: str = "4"
SPEC_VERSION_FIELD: str = "spec_version"
TASK_ID_FIELD: str = "task_id"
STEPS_FIELD: str = "steps"

STATUS_FIELD: str = "status"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_COMPLETED: str = "completed"

STARTED_AT_FIELD: str = "started_at"
COMPLETED_AT_FIELD: str = "completed_at"
LAST_HEARTBEAT_AT_FIELD: str = "last_heartbeat_at"
HEARTBEAT_INTERVAL_FIELD: str = "heartbeat_interval_seconds"
EXPECTED_COMPLETION_FIELD: str = "expected_completion_at"
STEP_FIELD: str = "step"
WATCHDOG_ACTIVE_FIELD: str = "watchdog_active"

CODE_ST_E007: str = "ST-E007"
CODE_ST_E009: str = "ST-E009"
CODE_ST_W005: str = "ST-W005"
CODE_ST_W006: str = "ST-W006"
CODE_ST_W008: str = "ST-W008"


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


def _healthy_in_progress_step(*, now: datetime) -> dict[str, object]:
    return _in_progress_step(
        started_at=_iso_z(dt=now - timedelta(minutes=10)),
        last_heartbeat_at=_iso_z(dt=now - timedelta(seconds=5)),
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        expected_completion_at=_iso_z(dt=now + timedelta(hours=1)),
    )


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
    instance_id: str = MACHINE_INSTANCE_ID,
    destroyed_at: str | None = None,
    watchdog_active: object | None = True,
) -> Path:
    # Liveness is `instance_id` non-empty AND `destroyed_at` null/absent — there is no
    # `actual_status` field in the machine-log schema. `watchdog_active=None` omits the key.
    step_dir: Path = paths.step_logs_dir(task_id=task_id) / SETUP_MACHINES_STEP_DIR_NAME
    step_dir.mkdir(parents=True, exist_ok=True)
    log_path: Path = step_dir / MACHINE_LOG_FILENAME
    entry: dict[str, object] = {
        SPEC_VERSION_FIELD: MACHINE_SPEC_VERSION,
        "provider": MACHINE_PROVIDER,
        # Literal on purpose: this is the EXTERNAL machine-log schema the provisioner
        # writes, not an internal constant. Keying the fixture off the module's own
        # constant would make any rename self-consistent and green while production
        # read a field no real machine_log.json contains — the actual_status bug.
        "instance_id": instance_id,
        "vm_name": instance_id,
        "created_at": MACHINE_CREATED_AT,
        "ready_at": MACHINE_READY_AT,
        "destroyed_at": destroyed_at,
        "total_cost_usd": None,
    }
    if watchdog_active is not None:
        entry[WATCHDOG_ACTIVE_FIELD] = watchdog_active
        entry["watchdog_idle_timeout_seconds"] = WATCHDOG_IDLE_TIMEOUT_SECONDS
    log_path.write_text(json.dumps([entry]), encoding="utf-8")
    return log_path


def _build_task_with_tracker_spec_version(
    *,
    repo_root: Path,
    task_id: str,
    steps: list[dict[str, object]],
    spec_version: str = TRACKER_SPEC_VERSION,
) -> Path:
    # build_step_tracker() writes a v1 tracker (no spec_version); ST-E009 only applies to
    # trackers that declare one, so write the tracker directly here.
    build_task_folder(repo_root=repo_root, task_id=task_id)
    build_task_json(repo_root=repo_root, task_id=task_id)
    tracker_path: Path = paths.step_tracker_path(task_id=task_id)
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    tracker_path.write_text(
        json.dumps(
            {
                SPEC_VERSION_FIELD: spec_version,
                TASK_ID_FIELD: task_id,
                STEPS_FIELD: steps,
            },
        ),
        encoding="utf-8",
    )
    return tracker_path


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
    _write_machine_log(task_id=TASK_ID)

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
    _write_machine_log(task_id=TASK_ID, destroyed_at=MACHINE_DESTROYED_AT)

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
# Backward-compat: v1 tracker (no spec_version) missing liveness fields →
# silent skip, no ST-E009
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
    assert codes == [], f"a v1 tracker must be skipped silently; got {codes}"
    assert all(result.passed for result in results)


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
RESUME_SENTINEL_FIELD: str = "resume_sentinel"
RESUME_AFTER_FIELD: str = "resume_after"
LIVENESS_PROBE_FIELD: str = "liveness_probe"
PAUSE_COUNT_FIELD: str = "pause_count"
PAUSED_STEP_NUMBER: int = 7
LIVENESS_PROBE: str = "ssh FT-NC80-v3 tmux has-session -t train"
DEFAULT_MAX_PAUSE_COUNT: int = 12
CODE_ST_E008: str = "ST-E008"
CODE_ST_E010: str = "ST-E010"
CODE_ST_W009: str = "ST-W009"


def _paused_step(*, watchdog_active: object) -> dict[str, object]:
    # A paused step's heartbeat is intentionally stale (no one is driving it). With a watchdog it
    # must NOT be ghost-flagged; without one it must be flagged ST-E008. The healthy shape carries
    # a liveness_probe and a pause_count; tests that exercise ST-W009/ST-E010 mutate them.
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
        LIVENESS_PROBE_FIELD: LIVENESS_PROBE,
        PAUSE_COUNT_FIELD: 1,
    }


def test_paused_with_watchdog_is_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    # Hours after the stale heartbeat — a normal in_progress step would be ST-W005/E007 here.
    now: datetime = datetime(2026, 5, 20, 14, 0, 0, tzinfo=UTC)
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[_paused_step(watchdog_active=True)])
    _write_machine_log(task_id=TASK_ID, watchdog_active=True)

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


# ---------------------------------------------------------------------------
# ST-E009: on a tracker that declares spec_version, an in_progress step whose
# liveness fields are missing or of the wrong type is unmonitorable → error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    [
        LAST_HEARTBEAT_AT_FIELD,
        HEARTBEAT_INTERVAL_FIELD,
        EXPECTED_COMPLETION_FIELD,
    ],
)
def test_st_e009_missing_liveness_field_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing_field: str,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    step: dict[str, object] = _healthy_in_progress_step(now=now)
    del step[missing_field]

    _build_task_with_tracker_spec_version(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[step],
    )

    result: VerificationResult = _as_results_list(_invoke(task_id=TASK_ID, now=now))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_E009 in codes, f"missing {missing_field} on a v2+ tracker must raise ST-E009"
    assert not result.passed, "ST-E009 is an error, so the verificator must not pass"


@pytest.mark.parametrize(
    ("field_name", "wrong_value"),
    [
        (LAST_HEARTBEAT_AT_FIELD, 1747742400),
        (HEARTBEAT_INTERVAL_FIELD, "300"),
        (EXPECTED_COMPLETION_FIELD, False),
    ],
)
def test_st_e009_wrong_type_liveness_field_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field_name: str,
    wrong_value: object,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    step: dict[str, object] = _healthy_in_progress_step(now=now)
    step[field_name] = wrong_value

    _build_task_with_tracker_spec_version(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[step],
    )

    result: VerificationResult = _as_results_list(_invoke(task_id=TASK_ID, now=now))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_E009 in codes, (
        f"{field_name}={wrong_value!r} on a v2+ tracker must raise ST-E009; got {codes}"
    )
    assert not result.passed, "ST-E009 is an error, so the verificator must not pass"


def test_st_e009_not_raised_for_healthy_v2_tracker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    _build_task_with_tracker_spec_version(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[_healthy_in_progress_step(now=now)],
    )

    result: VerificationResult = _as_results_list(_invoke(task_id=TASK_ID, now=now))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_E009 not in codes, f"a complete v2+ step must not raise ST-E009; got {codes}"


def test_st_e009_not_raised_on_v1_tracker_without_spec_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Backward-compatibility carve-out: a tracker with NO spec_version whose in_progress step
    # lacks the liveness fields produces no diagnostic at all.
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=now - timedelta(hours=10)),
                last_heartbeat_at=None,
                heartbeat_interval_seconds=None,
                expected_completion_at=None,
            ),
        ],
    )

    result: VerificationResult = _as_results_list(_invoke(task_id=TASK_ID, now=now))[0]
    codes: list[str] = _codes(result)
    assert codes == [], f"a v1 tracker must emit neither errors nor warnings; got {codes}"
    assert result.passed


# ---------------------------------------------------------------------------
# ST-W008: live VM whose machine_log entry does not record watchdog_active=true
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "watchdog_active",
    [
        None,  # key absent entirely
        False,
        "true",  # non-bool
    ],
)
def test_st_w008_live_vm_without_watchdog_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    watchdog_active: object | None,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[_healthy_in_progress_step(now=now)],
    )
    _write_machine_log(task_id=TASK_ID, watchdog_active=watchdog_active)

    result: VerificationResult = _as_results_list(_invoke(task_id=TASK_ID, now=now))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_W008 in codes, (
        f"a live VM with watchdog_active={watchdog_active!r} must raise ST-W008; got {codes}"
    )


def test_st_w008_absent_when_live_vm_has_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[_healthy_in_progress_step(now=now)],
    )
    _write_machine_log(task_id=TASK_ID, watchdog_active=True)

    result: VerificationResult = _as_results_list(_invoke(task_id=TASK_ID, now=now))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_W008 not in codes, (
        f"a watchdog-protected live VM must not raise ST-W008; got {codes}"
    )


def test_st_w008_absent_for_destroyed_vm_without_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[_healthy_in_progress_step(now=now)],
    )
    _write_machine_log(
        task_id=TASK_ID,
        destroyed_at=MACHINE_DESTROYED_AT,
        watchdog_active=None,
    )

    result: VerificationResult = _as_results_list(_invoke(task_id=TASK_ID, now=now))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_W008 not in codes, f"a destroyed VM must never raise ST-W008; got {codes}"


# ---------------------------------------------------------------------------
# Regression pin: a machine_log entry with ONLY real schema fields (no
# `actual_status` anywhere) must still be detected as a live VM.
# ---------------------------------------------------------------------------


def test_live_vm_detected_from_real_schema_fields_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    stale_heartbeat: datetime = now - timedelta(hours=1)

    _build_task(
        repo_root=tmp_path,
        task_id=TASK_ID,
        steps=[
            _in_progress_step(
                started_at=_iso_z(dt=now - timedelta(hours=2)),
                last_heartbeat_at=_iso_z(dt=stale_heartbeat),
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                expected_completion_at=_iso_z(dt=now + timedelta(hours=1)),
            ),
        ],
    )
    log_path: Path = _write_machine_log(task_id=TASK_ID)

    raw_log: str = log_path.read_text(encoding="utf-8")
    assert "actual_status" not in raw_log, (
        "the fixture must reproduce a real machine_log.json, which never carries actual_status"
    )

    result: VerificationResult = _as_results_list(_invoke(task_id=TASK_ID, now=now))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_E007 in codes, (
        f"instance_id set + destroyed_at null means live: expected ST-E007, got {codes}"
    )
    assert CODE_ST_W005 not in codes, f"a live VM must not be downgraded to ST-W005; got {codes}"
    assert not result.passed, "an idle-billing live VM must make the verificator exit non-zero"


# ---------------------------------------------------------------------------
# ST-E010: a paused_waiting step that has re-paused past the cap is not waiting,
# it is stuck — and no probe can catch the case where no probe was recorded
# ---------------------------------------------------------------------------

PAUSED_NOW: datetime = datetime(2026, 5, 20, 14, 0, 0, tzinfo=UTC)


def _invoke_with_cap(
    *,
    now: datetime,
    max_pause_count: int,
) -> VerificationResult:
    return _as_results_list(
        verify_step_liveness(
            task_id=TASK_ID,
            now=now,
            slow_factor=2.0,
            max_pause_count=max_pause_count,
        ),
    )[0]


def test_st_e010_pause_count_above_cap_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    step: dict[str, object] = _paused_step(watchdog_active=True)
    step[PAUSE_COUNT_FIELD] = DEFAULT_MAX_PAUSE_COUNT + 1
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])

    result: VerificationResult = _as_results_list(_invoke(now=PAUSED_NOW))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_E010 in codes, f"pause_count past the cap must raise ST-E010; got {codes}"
    assert not result.passed, "ST-E010 is an error, so the verificator must not pass"


def test_st_e010_not_raised_exactly_at_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    step: dict[str, object] = _paused_step(watchdog_active=True)
    step[PAUSE_COUNT_FIELD] = DEFAULT_MAX_PAUSE_COUNT
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])

    codes: list[str] = _codes(_as_results_list(_invoke(now=PAUSED_NOW))[0])
    assert CODE_ST_E010 not in codes, f"the cap itself is still allowed; got {codes}"


@pytest.mark.parametrize(
    ("max_pause_count", "expect_flagged"),
    [
        (4, True),
        (5, False),
    ],
)
def test_st_e010_honours_max_pause_count_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    max_pause_count: int,
    expect_flagged: bool,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    step: dict[str, object] = _paused_step(watchdog_active=True)
    step[PAUSE_COUNT_FIELD] = 5
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])

    codes: list[str] = _codes(
        _invoke_with_cap(now=PAUSED_NOW, max_pause_count=max_pause_count),
    )
    assert (CODE_ST_E010 in codes) is expect_flagged, (
        f"pause_count=5 with cap {max_pause_count} should flag={expect_flagged}; got {codes}"
    )


def test_st_e010_cli_exposes_max_pause_count_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    step: dict[str, object] = _paused_step(watchdog_active=True)
    step[PAUSE_COUNT_FIELD] = 5
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_step_liveness", TASK_ID, "--max-pause-count", "4"],
    )

    with pytest.raises(SystemExit) as exit_info:
        verify_step_liveness_module.main()
    assert exit_info.value.code == 1, "ST-E010 is an error, so the CLI must exit non-zero"


@pytest.mark.parametrize("pause_count", [None, "13", 13.5])
def test_st_e010_not_raised_for_non_int_pause_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pause_count: object,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    step: dict[str, object] = _paused_step(watchdog_active=True)
    step[PAUSE_COUNT_FIELD] = pause_count
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])

    codes: list[str] = _codes(_as_results_list(_invoke(now=PAUSED_NOW))[0])
    assert CODE_ST_E010 not in codes, (
        f"pause_count={pause_count!r} is not a count, so ST-E010 must not fire; got {codes}"
    )


def test_st_e010_not_raised_for_missing_pause_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    step: dict[str, object] = _paused_step(watchdog_active=True)
    del step[PAUSE_COUNT_FIELD]
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])

    codes: list[str] = _codes(_as_results_list(_invoke(now=PAUSED_NOW))[0])
    assert CODE_ST_E010 not in codes, f"an absent pause_count must not fire ST-E010; got {codes}"


def test_st_e010_not_raised_for_in_progress_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # pause_count is meaningless outside paused_waiting; a stray value must not be judged.
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    step: dict[str, object] = _healthy_in_progress_step(now=now)
    step[PAUSE_COUNT_FIELD] = 99
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])

    codes: list[str] = _codes(_as_results_list(_invoke(now=now))[0])
    assert CODE_ST_E010 not in codes, f"ST-E010 applies only to paused steps; got {codes}"


# ---------------------------------------------------------------------------
# ST-W009: a paused_waiting step with no liveness_probe cannot tell a dead job
# from a running one, so it re-pauses forever — a warning, never a blocker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("liveness_probe", [None, ""])
def test_st_w009_missing_probe_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    liveness_probe: object,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    step: dict[str, object] = _paused_step(watchdog_active=True)
    step[LIVENESS_PROBE_FIELD] = liveness_probe
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])

    result: VerificationResult = _as_results_list(_invoke(now=PAUSED_NOW))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_W009 in codes, (
        f"liveness_probe={liveness_probe!r} must raise ST-W009; got {codes}"
    )
    assert result.passed, "ST-W009 is a warning and must not block on its own"


def test_st_w009_absent_probe_key_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    step: dict[str, object] = _paused_step(watchdog_active=True)
    del step[LIVENESS_PROBE_FIELD]
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[step])

    result: VerificationResult = _as_results_list(_invoke(now=PAUSED_NOW))[0]
    codes: list[str] = _codes(result)
    assert CODE_ST_W009 in codes, f"an absent liveness_probe must raise ST-W009; got {codes}"
    assert result.passed, "ST-W009 is a warning and must not block on its own"


def test_st_w009_absent_when_probe_recorded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[_paused_step(watchdog_active=True)])

    codes: list[str] = _codes(_as_results_list(_invoke(now=PAUSED_NOW))[0])
    assert CODE_ST_W009 not in codes, f"a recorded probe must not raise ST-W009; got {codes}"


def test_st_w009_not_raised_for_in_progress_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # An in_progress step has an owner driving it; it needs no probe.
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    _build_task(repo_root=tmp_path, task_id=TASK_ID, steps=[_healthy_in_progress_step(now=now)])

    codes: list[str] = _codes(_as_results_list(_invoke(now=now))[0])
    assert CODE_ST_W009 not in codes, f"ST-W009 applies only to paused steps; got {codes}"
