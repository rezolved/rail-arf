"""Tests for the `metrics` corrections target kind (S-0055-03).

Covers `build_metrics_target_id`/`parse_metrics_target_id` round-tripping, `load_target_record`
resolving a `metrics` `TargetKey` against `results/metrics.json`, and
`aggregate_metric_results_short` honoring `update`/`delete`/`replace` corrections against metric
entries the same way `aggregate_predictions` already honors them for predictions assets.
"""

from pathlib import Path

import pytest

import arf.scripts.aggregators.aggregate_metric_results as agg_mod
from arf.scripts.aggregators.aggregate_metric_results import (
    MetricResultsShort,
    aggregate_metric_results_short,
)
from arf.scripts.common.artifacts import (
    ALL_TARGET_KINDS,
    ASSET_TARGET_KINDS,
    TARGET_KIND_METRICS,
    TargetKey,
    load_target_record,
    supports_file_changes,
)
from arf.scripts.common.task_metrics import (
    IMPLICIT_VARIANT_ID,
    build_metrics_target_id,
    parse_metrics_target_id,
)
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.writers import write_json

METRIC_KEY: str = "targeted_benchmark_score"
TASK_ORIGINAL: str = "t0001_base_benchmark"
TASK_REJUDGE: str = "t0002_rejudge_benchmark"
VARIANT_QWEN_BASE: str = "qwen35-base"
VARIANT_LABEL_QWEN_BASE: str = "Qwen3.5-35B-A3B base"
CORRECTION_ID_UPDATE: str = "C-0002-01"
CORRECTION_ID_DELETE: str = "C-0002-01"
CORRECTION_ID_REPLACE: str = "C-0002-01"
RATIONALE_COMPROMISED: str = "Predictions were regenerated; old score is stale pending rejudge."


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        aggregator_modules=[agg_mod],
    )
    return tmp_path


def _write_task(*, repo_root: Path, task_id: str) -> None:
    write_json(
        path=repo_root / "tasks" / task_id / "task.json",
        data={
            "spec_version": "5",
            "name": task_id,
            "short_description": "test",
            "long_description": "test",
            "status": "completed",
            "dependencies": [],
        },
    )


def _write_metrics(
    *,
    repo_root: Path,
    task_id: str,
    metrics_payload: dict[str, object],
) -> None:
    write_json(
        path=repo_root / "tasks" / task_id / "results" / "metrics.json",
        data=metrics_payload,
    )


def _write_correction(
    *,
    repo_root: Path,
    correcting_task: str,
    file_name: str,
    payload: dict[str, object],
) -> None:
    write_json(
        path=repo_root / "tasks" / correcting_task / "corrections" / file_name,
        data=payload,
    )


# ---------------------------------------------------------------------------
# target_id build/parse
# ---------------------------------------------------------------------------


def test_build_and_parse_target_id_round_trips_explicit_variant() -> None:
    target_id: str = build_metrics_target_id(
        metric_key=METRIC_KEY,
        variant_id=VARIANT_QWEN_BASE,
    )
    parsed = parse_metrics_target_id(target_id=target_id)
    assert parsed is not None
    assert parsed.metric_key == METRIC_KEY
    assert parsed.variant_id == VARIANT_QWEN_BASE


def test_build_and_parse_target_id_round_trips_implicit_variant() -> None:
    target_id: str = build_metrics_target_id(
        metric_key=METRIC_KEY,
        variant_id=IMPLICIT_VARIANT_ID,
    )
    parsed = parse_metrics_target_id(target_id=target_id)
    assert parsed is not None
    assert parsed.metric_key == METRIC_KEY
    assert parsed.variant_id == IMPLICIT_VARIANT_ID


def test_parse_target_id_rejects_missing_separator() -> None:
    assert parse_metrics_target_id(target_id=METRIC_KEY) is None


# ---------------------------------------------------------------------------
# target_kind registration
# ---------------------------------------------------------------------------


def test_metrics_target_kind_registered_without_file_support() -> None:
    assert TARGET_KIND_METRICS in ALL_TARGET_KINDS
    assert TARGET_KIND_METRICS not in ASSET_TARGET_KINDS
    assert supports_file_changes(target_kind=TARGET_KIND_METRICS) is False


# ---------------------------------------------------------------------------
# load_target_record
# ---------------------------------------------------------------------------


def test_load_target_record_finds_explicit_variant_metric(repo: Path) -> None:
    _write_task(repo_root=repo, task_id=TASK_ORIGINAL)
    _write_metrics(
        repo_root=repo,
        task_id=TASK_ORIGINAL,
        metrics_payload={
            "variants": [
                {
                    "variant_id": VARIANT_QWEN_BASE,
                    "label": VARIANT_LABEL_QWEN_BASE,
                    "dimensions": {},
                    "metrics": {METRIC_KEY: 0.42},
                },
            ],
        },
    )

    record = load_target_record(
        key=TargetKey(
            task_id=TASK_ORIGINAL,
            target_kind=TARGET_KIND_METRICS,
            target_id=build_metrics_target_id(
                metric_key=METRIC_KEY,
                variant_id=VARIANT_QWEN_BASE,
            ),
        ),
    )

    assert record is not None
    assert record.payload["value"] == 0.42
    assert record.payload["variant_label"] == VARIANT_LABEL_QWEN_BASE
    assert record.file_entries == []


def test_load_target_record_returns_none_for_missing_metric(repo: Path) -> None:
    _write_task(repo_root=repo, task_id=TASK_ORIGINAL)
    _write_metrics(
        repo_root=repo,
        task_id=TASK_ORIGINAL,
        metrics_payload={METRIC_KEY: 0.42},
    )

    record = load_target_record(
        key=TargetKey(
            task_id=TASK_ORIGINAL,
            target_kind=TARGET_KIND_METRICS,
            target_id=build_metrics_target_id(
                metric_key="nonexistent_metric",
                variant_id=IMPLICIT_VARIANT_ID,
            ),
        ),
    )

    assert record is None


# ---------------------------------------------------------------------------
# aggregate_metric_results_short + corrections
# ---------------------------------------------------------------------------


def test_update_correction_overrides_aggregated_value(repo: Path) -> None:
    _write_task(repo_root=repo, task_id=TASK_ORIGINAL)
    _write_task(repo_root=repo, task_id=TASK_REJUDGE)
    _write_metrics(
        repo_root=repo,
        task_id=TASK_ORIGINAL,
        metrics_payload={METRIC_KEY: 0.42},
    )
    implicit_target_id: str = build_metrics_target_id(
        metric_key=METRIC_KEY,
        variant_id=IMPLICIT_VARIANT_ID,
    )
    _write_correction(
        repo_root=repo,
        correcting_task=TASK_REJUDGE,
        file_name=f"metrics_{implicit_target_id}.json",
        payload={
            "spec_version": "4",
            "correction_id": CORRECTION_ID_UPDATE,
            "correcting_task": TASK_REJUDGE,
            "target_task": TASK_ORIGINAL,
            "target_kind": TARGET_KIND_METRICS,
            "target_id": implicit_target_id,
            "action": "update",
            "changes": {"value": None},
            "rationale": RATIONALE_COMPROMISED,
        },
    )

    result: list[MetricResultsShort] = aggregate_metric_results_short()

    assert len(result) == 1
    assert result[0].entries[0].value is None


def test_delete_correction_removes_entry_from_aggregate(repo: Path) -> None:
    _write_task(repo_root=repo, task_id=TASK_ORIGINAL)
    _write_task(repo_root=repo, task_id=TASK_REJUDGE)
    _write_metrics(
        repo_root=repo,
        task_id=TASK_ORIGINAL,
        metrics_payload={METRIC_KEY: 0.42},
    )
    implicit_target_id: str = build_metrics_target_id(
        metric_key=METRIC_KEY,
        variant_id=IMPLICIT_VARIANT_ID,
    )
    _write_correction(
        repo_root=repo,
        correcting_task=TASK_REJUDGE,
        file_name=f"metrics_{implicit_target_id}.json",
        payload={
            "spec_version": "4",
            "correction_id": CORRECTION_ID_DELETE,
            "correcting_task": TASK_REJUDGE,
            "target_task": TASK_ORIGINAL,
            "target_kind": TARGET_KIND_METRICS,
            "target_id": implicit_target_id,
            "action": "delete",
            "changes": None,
            "rationale": RATIONALE_COMPROMISED,
        },
    )

    result: list[MetricResultsShort] = aggregate_metric_results_short()

    assert len(result) == 0


def test_replace_correction_redirects_to_rejudge_task_entry(repo: Path) -> None:
    _write_task(repo_root=repo, task_id=TASK_ORIGINAL)
    _write_task(repo_root=repo, task_id=TASK_REJUDGE)
    _write_metrics(
        repo_root=repo,
        task_id=TASK_ORIGINAL,
        metrics_payload={
            "variants": [
                {
                    "variant_id": VARIANT_QWEN_BASE,
                    "label": VARIANT_LABEL_QWEN_BASE,
                    "dimensions": {},
                    "metrics": {METRIC_KEY: 0.42},
                },
            ],
        },
    )
    _write_metrics(
        repo_root=repo,
        task_id=TASK_REJUDGE,
        metrics_payload={
            "variants": [
                {
                    "variant_id": VARIANT_QWEN_BASE,
                    "label": VARIANT_LABEL_QWEN_BASE,
                    "dimensions": {},
                    "metrics": {METRIC_KEY: 0.61},
                },
            ],
        },
    )
    original_target_id: str = build_metrics_target_id(
        metric_key=METRIC_KEY,
        variant_id=VARIANT_QWEN_BASE,
    )
    _write_correction(
        repo_root=repo,
        correcting_task=TASK_REJUDGE,
        file_name=f"metrics_{original_target_id}.json",
        payload={
            "spec_version": "4",
            "correction_id": CORRECTION_ID_REPLACE,
            "correcting_task": TASK_REJUDGE,
            "target_task": TASK_ORIGINAL,
            "target_kind": TARGET_KIND_METRICS,
            "target_id": original_target_id,
            "action": "replace",
            "changes": {
                "replacement_task": TASK_REJUDGE,
                "replacement_id": original_target_id,
            },
            "rationale": "Superseded by the rejudge task's recomputed score.",
        },
    )

    result: list[MetricResultsShort] = aggregate_metric_results_short()

    assert len(result) == 1
    assert result[0].result_count == 1
    assert result[0].entries[0].task_id == TASK_REJUDGE
    assert result[0].entries[0].value == 0.61
