"""Tests for prestep step arming (``arf.scripts.utils.prestep``).

Contract from ``arf/specifications/step_tracker_specification.md`` v5, section
"Who writes the liveness fields":

* ``prestep`` writes all four liveness fields when it marks a step ``in_progress`` —
  ``current_owner``, ``last_heartbeat_at``, ``heartbeat_interval_seconds``,
  ``expected_completion_at`` — in addition to ``status`` / ``started_at`` / ``log_file``.
* ``prestep`` stamps ``spec_version`` on the tracker when absent and never overwrites an
  existing one. Already-completed steps are left byte-for-byte alone ("nothing rewrites
  history").
* Defaults are ``heartbeat_interval_seconds=1800`` and an expected duration of ``3600``
  seconds, both overridable by the caller.
* Arming is what makes a step monitorable: ``verify_step_liveness`` can only raise
  ``ST-E007`` on a step that carries the liveness fields.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import arf.scripts.utils.prestep as prestep_module
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
TASK_BRANCH: str = f"task/{TASK_ID}"

STEP_ID: str = "research-papers"
STEP_NUMBER: int = 4
IMPLEMENTATION_STEP_ID: str = "implementation"
IMPLEMENTATION_STEP_NUMBER: int = 8
SETUP_MACHINES_STEP_NUMBER: int = 7
SETUP_MACHINES_LOG_DIR_NAME: str = "007_setup-machines"
MACHINE_LOG_FILENAME: str = "machine_log.json"

SPEC_VERSION_FIELD: str = "spec_version"
STEPS_FIELD: str = "steps"
STEP_FIELD: str = "step"
NAME_FIELD: str = "name"
STATUS_FIELD: str = "status"
STARTED_AT_FIELD: str = "started_at"
COMPLETED_AT_FIELD: str = "completed_at"
LOG_FILE_FIELD: str = "log_file"
CURRENT_OWNER_FIELD: str = "current_owner"
LAST_HEARTBEAT_AT_FIELD: str = "last_heartbeat_at"
HEARTBEAT_INTERVAL_FIELD: str = "heartbeat_interval_seconds"
EXPECTED_COMPLETION_FIELD: str = "expected_completion_at"

STATUS_PENDING: str = "pending"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_COMPLETED: str = "completed"

DEFAULT_HEARTBEAT_INTERVAL_SECONDS: int = 1800
DEFAULT_EXPECTED_DURATION_SECONDS: int = 3600
OVERRIDE_HEARTBEAT_INTERVAL_SECONDS: int = 300
OVERRIDE_EXPECTED_DURATION_SECONDS: int = 21600
OVERRIDE_OWNER: str = "execute-task/implementation-subagent-pid42"

EXISTING_SPEC_VERSION: str = "3"
ISO_FORMAT: str = "%Y-%m-%dT%H:%M:%SZ"

CODE_ST_E007: str = "ST-E007"
GHOSTED_HOURS: int = 14

# CLI flag names the implementation must expose (see the report accompanying these tests).
CLI_OWNER_FLAG: str = "--current-owner"
CLI_INTERVAL_FLAG: str = "--heartbeat-interval-seconds"
CLI_DURATION_FLAG: str = "--expected-duration-seconds"


def _setup(*, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=repo_root,
        verificator_modules=[prestep_module, verify_step_liveness_module],
    )
    # Neutralize the git-state preconditions: they are orthogonal to liveness arming.
    monkeypatch.setattr(prestep_module, "_detect_repo_root", lambda: repo_root)
    monkeypatch.setattr(
        prestep_module,
        "_get_current_branch",
        lambda *, repo_root: TASK_BRANCH,
    )
    monkeypatch.setattr(
        prestep_module,
        "_is_working_tree_clean",
        lambda *, repo_root: True,
    )


def _parse_iso_z(*, value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=UTC)


def _iso_z(*, moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(ISO_FORMAT)


def _completed_step(*, step_number: int, name: str) -> dict[str, object]:
    return {
        STEP_FIELD: step_number,
        NAME_FIELD: name,
        "description": f"Completed step {name}.",
        STATUS_FIELD: STATUS_COMPLETED,
        STARTED_AT_FIELD: "2026-04-01T00:00:00Z",
        COMPLETED_AT_FIELD: "2026-04-01T00:10:00Z",
        LOG_FILE_FIELD: f"logs/steps/{step_number:03d}_{name}/",
    }


def _pending_step(*, step_number: int, name: str) -> dict[str, object]:
    return {
        STEP_FIELD: step_number,
        NAME_FIELD: name,
        "description": f"Pending step {name}.",
        STATUS_FIELD: STATUS_PENDING,
        STARTED_AT_FIELD: None,
        COMPLETED_AT_FIELD: None,
        LOG_FILE_FIELD: None,
    }


def _default_steps() -> list[dict[str, object]]:
    return [
        _completed_step(step_number=1, name="create-branch"),
        _completed_step(step_number=2, name="check-deps"),
        _completed_step(step_number=3, name="init-task"),
        _pending_step(step_number=STEP_NUMBER, name=STEP_ID),
    ]


def _implementation_steps() -> list[dict[str, object]]:
    steps: list[dict[str, object]] = [
        _completed_step(step_number=number, name=f"earlier-step-{number}")
        for number in range(1, SETUP_MACHINES_STEP_NUMBER)
    ]
    steps.append(_completed_step(step_number=SETUP_MACHINES_STEP_NUMBER, name="setup-machines"))
    steps.append(
        _pending_step(
            step_number=IMPLEMENTATION_STEP_NUMBER,
            name=IMPLEMENTATION_STEP_ID,
        ),
    )
    return steps


def _build_task(
    *,
    repo_root: Path,
    steps: list[dict[str, object]],
    spec_version: str | None = None,
) -> Path:
    build_task_folder(repo_root=repo_root, task_id=TASK_ID)
    build_task_json(repo_root=repo_root, task_id=TASK_ID)
    tracker_path: Path = build_step_tracker(
        repo_root=repo_root,
        task_id=TASK_ID,
        steps=steps,
    )
    if spec_version is not None:
        tracker: dict[str, object] = _read_tracker()
        tracker[SPEC_VERSION_FIELD] = spec_version
        tracker_path.write_text(json.dumps(tracker, indent=2) + "\n", encoding="utf-8")
    return tracker_path


def _read_tracker() -> dict[str, object]:
    tracker_path: Path = paths.step_tracker_path(task_id=TASK_ID)
    data: object = json.loads(tracker_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "tracker root is a JSON object"
    return data


def _get_step(*, tracker: dict[str, object], step_number: int) -> dict[str, object]:
    steps: object = tracker[STEPS_FIELD]
    assert isinstance(steps, list), "steps is a list"
    for step in steps:
        assert isinstance(step, dict), "step entry is a dict"
        if step[STEP_FIELD] == step_number:
            return step
    raise AssertionError(f"step {step_number} not found in tracker")


def _write_machine_log(*, live: bool) -> Path:
    step_dir: Path = paths.step_logs_dir(task_id=TASK_ID) / SETUP_MACHINES_LOG_DIR_NAME
    step_dir.mkdir(parents=True, exist_ok=True)
    log_path: Path = step_dir / MACHINE_LOG_FILENAME
    entry: dict[str, object] = {
        SPEC_VERSION_FIELD: "6",
        "provider": "azure_ml",
        "instance_id": "arf-NC80-weu-v1",
        "vm_name": "arf-NC80-weu-v1",
        "created_at": "2026-05-20T08:00:00Z",
        "ready_at": "2026-05-20T08:06:40Z",
        "destroyed_at": None if live else "2026-05-20T09:00:00Z",
        "total_cost_usd": None,
        "watchdog_active": True,
        "watchdog_idle_timeout_seconds": 1800,
    }
    log_path.write_text(json.dumps([entry]), encoding="utf-8")
    return log_path


def _as_results_list(
    result: VerificationResult | list[VerificationResult],
) -> list[VerificationResult]:
    if isinstance(result, list):
        return result
    return [result]


def _all_codes(*, results: list[VerificationResult]) -> list[str]:
    codes: list[str] = []
    for result in results:
        codes.extend(diagnostic.code.text for diagnostic in result.diagnostics)
    return codes


# ---------------------------------------------------------------------------
# A: prestep writes status/started_at/log_file AND all four liveness fields
# ---------------------------------------------------------------------------


def test_prestep_still_writes_status_started_at_and_log_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    exit_code: int = prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)
    assert exit_code == 0, "prestep must succeed once its preconditions are met"

    step: dict[str, object] = _get_step(tracker=_read_tracker(), step_number=STEP_NUMBER)
    assert step[STATUS_FIELD] == STATUS_IN_PROGRESS

    started_at: object = step[STARTED_AT_FIELD]
    assert isinstance(started_at, str), "started_at is an ISO 8601 string"
    _parse_iso_z(value=started_at)

    log_file: object = step[LOG_FILE_FIELD]
    assert isinstance(log_file, str), "log_file is a string"
    assert f"{STEP_NUMBER:03d}_{STEP_ID}" in log_file


def test_prestep_writes_all_four_liveness_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)

    step: dict[str, object] = _get_step(tracker=_read_tracker(), step_number=STEP_NUMBER)
    for field_name in [
        CURRENT_OWNER_FIELD,
        LAST_HEARTBEAT_AT_FIELD,
        HEARTBEAT_INTERVAL_FIELD,
        EXPECTED_COMPLETION_FIELD,
    ]:
        assert field_name in step, f"prestep must write {field_name} when arming a step"
        assert step[field_name] is not None, f"{field_name} must not be null on an armed step"


def test_prestep_last_heartbeat_equals_started_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The step has just proven itself alive, so its first heartbeat is its start.
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)

    step: dict[str, object] = _get_step(tracker=_read_tracker(), step_number=STEP_NUMBER)
    assert step[LAST_HEARTBEAT_AT_FIELD] == step[STARTED_AT_FIELD]


def test_prestep_expected_completion_is_started_at_plus_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)

    step: dict[str, object] = _get_step(tracker=_read_tracker(), step_number=STEP_NUMBER)
    started_at: object = step[STARTED_AT_FIELD]
    expected_completion_at: object = step[EXPECTED_COMPLETION_FIELD]
    assert isinstance(started_at, str), "started_at is an ISO 8601 string"
    assert isinstance(expected_completion_at, str), "expected_completion_at is an ISO 8601 string"

    delta: timedelta = _parse_iso_z(value=expected_completion_at) - _parse_iso_z(value=started_at)
    assert delta.total_seconds() == DEFAULT_EXPECTED_DURATION_SECONDS


# ---------------------------------------------------------------------------
# B: spec_version stamping and the mid-flight upgrade
# ---------------------------------------------------------------------------


def test_prestep_stamps_spec_version_when_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())
    assert SPEC_VERSION_FIELD not in _read_tracker(), "fixture starts as a v1 tracker"

    prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)

    tracker: dict[str, object] = _read_tracker()
    spec_version: object = tracker.get(SPEC_VERSION_FIELD)
    assert isinstance(spec_version, str), "spec_version is stamped as a string"
    assert len(spec_version) > 0


def test_prestep_does_not_overwrite_existing_spec_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(
        repo_root=tmp_path,
        steps=_default_steps(),
        spec_version=EXISTING_SPEC_VERSION,
    )

    prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)

    assert _read_tracker().get(SPEC_VERSION_FIELD) == EXISTING_SPEC_VERSION


def test_prestep_upgrade_leaves_completed_steps_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Mid-flight upgrade: a v1 tracker whose completed steps carry no liveness fields.
    # Only the newly-started step gains them. Nothing rewrites history.
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    tracker_before: dict[str, object] = _read_tracker()
    completed_before: list[dict[str, object]] = [
        _get_step(tracker=tracker_before, step_number=number) for number in [1, 2, 3]
    ]
    for step_before in completed_before:
        assert LAST_HEARTBEAT_AT_FIELD not in step_before, "fixture completed steps are pre-v2"

    prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)

    tracker_after: dict[str, object] = _read_tracker()
    assert SPEC_VERSION_FIELD in tracker_after, "the tracker is upgraded in place"

    armed_step: dict[str, object] = _get_step(
        tracker=tracker_after,
        step_number=STEP_NUMBER,
    )
    for field_name in [
        CURRENT_OWNER_FIELD,
        LAST_HEARTBEAT_AT_FIELD,
        HEARTBEAT_INTERVAL_FIELD,
        EXPECTED_COMPLETION_FIELD,
    ]:
        assert armed_step.get(field_name) is not None, f"armed step carries {field_name}"

    completed_after: list[dict[str, object]] = [
        _get_step(tracker=tracker_after, step_number=number) for number in [1, 2, 3]
    ]
    assert completed_after == completed_before, "completed steps must be byte-for-byte unchanged"


# ---------------------------------------------------------------------------
# C: defaults and overrides
# ---------------------------------------------------------------------------


def test_prestep_liveness_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)

    step: dict[str, object] = _get_step(tracker=_read_tracker(), step_number=STEP_NUMBER)
    assert step[HEARTBEAT_INTERVAL_FIELD] == DEFAULT_HEARTBEAT_INTERVAL_SECONDS


def test_prestep_default_owner_identifies_the_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    prestep_module.run_prestep(task_id=TASK_ID, step_id=STEP_ID)

    step: dict[str, object] = _get_step(tracker=_read_tracker(), step_number=STEP_NUMBER)
    current_owner: object = step[CURRENT_OWNER_FIELD]
    assert isinstance(current_owner, str), "current_owner is a string"
    assert len(current_owner) > 0, "current_owner must never be empty on an armed step"
    assert current_owner == f"{prestep_module.DEFAULT_OWNER_PREFIX}{STEP_ID}", (
        "the default owner is <driver>/<step_id> — the format the verificator and "
        "/diagnose-stuck-step render back"
    )


def test_prestep_overrides_interval_and_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    prestep_module.run_prestep(
        task_id=TASK_ID,
        step_id=STEP_ID,
        current_owner=OVERRIDE_OWNER,
        heartbeat_interval_seconds=OVERRIDE_HEARTBEAT_INTERVAL_SECONDS,
        expected_duration_seconds=OVERRIDE_EXPECTED_DURATION_SECONDS,
    )

    step: dict[str, object] = _get_step(tracker=_read_tracker(), step_number=STEP_NUMBER)
    assert step[HEARTBEAT_INTERVAL_FIELD] == OVERRIDE_HEARTBEAT_INTERVAL_SECONDS
    assert step[CURRENT_OWNER_FIELD] == OVERRIDE_OWNER

    started_at: object = step[STARTED_AT_FIELD]
    expected_completion_at: object = step[EXPECTED_COMPLETION_FIELD]
    assert isinstance(started_at, str), "started_at is an ISO 8601 string"
    assert isinstance(expected_completion_at, str), "expected_completion_at is an ISO 8601 string"
    delta: timedelta = _parse_iso_z(value=expected_completion_at) - _parse_iso_z(value=started_at)
    assert delta.total_seconds() == OVERRIDE_EXPECTED_DURATION_SECONDS


def test_prestep_cli_exposes_liveness_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_default_steps())

    monkeypatch.setattr(
        "sys.argv",
        [
            "prestep",
            TASK_ID,
            STEP_ID,
            CLI_OWNER_FLAG,
            OVERRIDE_OWNER,
            CLI_INTERVAL_FLAG,
            str(OVERRIDE_HEARTBEAT_INTERVAL_SECONDS),
            CLI_DURATION_FLAG,
            str(OVERRIDE_EXPECTED_DURATION_SECONDS),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        prestep_module.main()
    assert exc_info.value.code == 0

    step: dict[str, object] = _get_step(tracker=_read_tracker(), step_number=STEP_NUMBER)
    assert step[CURRENT_OWNER_FIELD] == OVERRIDE_OWNER
    assert step[HEARTBEAT_INTERVAL_FIELD] == OVERRIDE_HEARTBEAT_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# E: regression pin — the 14-hour ghosted step with a live VM
# ---------------------------------------------------------------------------


def _ghost_the_armed_step(*, now: datetime) -> None:
    # Simulate an owner that armed the step and then never heartbeated again.
    tracker: dict[str, object] = _read_tracker()
    step: dict[str, object] = _get_step(
        tracker=tracker,
        step_number=IMPLEMENTATION_STEP_NUMBER,
    )
    started_at: datetime = now - timedelta(hours=GHOSTED_HOURS)
    step[STARTED_AT_FIELD] = _iso_z(moment=started_at)
    step[LAST_HEARTBEAT_AT_FIELD] = _iso_z(moment=started_at)
    step[EXPECTED_COMPLETION_FIELD] = _iso_z(moment=started_at + timedelta(hours=1))
    paths.step_tracker_path(task_id=TASK_ID).write_text(
        json.dumps(tracker, indent=2) + "\n",
        encoding="utf-8",
    )


def test_ghosted_armed_step_with_live_vm_errors_st_e007(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The exact shape that went undetected for 14 hours: step 8 armed by prestep, no
    # heartbeat since, and a machine_log entry with a non-empty instance_id and no
    # destroyed_at — a live H100 still billing.
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    _build_task(repo_root=tmp_path, steps=_implementation_steps())
    now: datetime = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)

    prestep_module.run_prestep(task_id=TASK_ID, step_id=IMPLEMENTATION_STEP_ID)
    _ghost_the_armed_step(now=now)
    _write_machine_log(live=True)

    results: list[VerificationResult] = _as_results_list(
        verify_step_liveness(task_id=TASK_ID, now=now, slow_factor=2.0),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_E007 in codes, (
        f"a {GHOSTED_HOURS}h-silent armed step with a live VM must raise ST-E007; got {codes}"
    )
    assert not all(result.passed for result in results), (
        "ST-E007 is an error: the verificator must fail, not warn and exit 0"
    )


def test_unarmed_step_yields_no_st_e007(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The pre-arming world, verbatim: no spec_version, no liveness fields on the
    # in_progress step. The very same live VM produces no error at all — this is
    # precisely what prestep's arming buys.
    _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
    now: datetime = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
    started_at: str = _iso_z(moment=now - timedelta(hours=GHOSTED_HOURS))

    steps: list[dict[str, object]] = _implementation_steps()
    unarmed_step: dict[str, object] = steps[-1]
    unarmed_step[STATUS_FIELD] = STATUS_IN_PROGRESS
    unarmed_step[STARTED_AT_FIELD] = started_at

    _build_task(repo_root=tmp_path, steps=steps)
    _write_machine_log(live=True)

    results: list[VerificationResult] = _as_results_list(
        verify_step_liveness(task_id=TASK_ID, now=now, slow_factor=2.0),
    )
    codes: list[str] = _all_codes(results=results)
    assert CODE_ST_E007 not in codes, (
        f"an unarmed v1 step is unmonitorable, so ST-E007 cannot fire; got {codes}"
    )
