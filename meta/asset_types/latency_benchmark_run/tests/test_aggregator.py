"""Tests for the latency_benchmark_run aggregator."""

import json
from pathlib import Path

import pytest

import meta.asset_types.latency_benchmark_run.aggregator as agg_mod
from arf.tests.fixtures.asset_builders.latency_benchmark_run import (
    build_latency_benchmark_run_asset,
)
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.task_builder import build_complete_task

TASK_A: str = "t0001_alpha"
TASK_B: str = "t0002_bravo"
RUN_A: str = "nebius_fast_20260520_1430_a"
RUN_B: str = "vllm_fp8_v1_20260601_0930_a"
CATEGORY_LATENCY: str = "latency-benchmark"
CATEGORY_OTHER: str = "fp8-quantization"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        aggregator_modules=[agg_mod],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_empty_returns_no_runs_short(repo: Path) -> None:
    result = agg_mod.aggregate_latency_benchmark_runs_short()
    assert len(result) == 0


def test_empty_returns_no_runs_full(repo: Path) -> None:
    result = agg_mod.aggregate_latency_benchmark_runs_full()
    assert len(result) == 0


def test_empty_json_format(repo: Path) -> None:
    runs = agg_mod.aggregate_latency_benchmark_runs_short()
    output: str = agg_mod._format_short_json(runs=runs)
    parsed: dict[str, object] = json.loads(output)
    assert parsed["run_count"] == 0
    assert parsed["runs"] == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discovers_runs_across_tasks(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_complete_task(repo_root=repo, task_id=TASK_B, task_index=2)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_B,
        run_id=RUN_B,
    )

    result = agg_mod.aggregate_latency_benchmark_runs_short()
    ids: list[str] = [r.run_id for r in result]
    assert RUN_A in ids
    assert RUN_B in ids
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Short vs full
# ---------------------------------------------------------------------------


def test_short_has_expected_fields(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )

    short = agg_mod.aggregate_latency_benchmark_runs_short()
    assert len(short) == 1
    s = short[0]
    assert s.run_id == RUN_A
    assert s.endpoint_kind == "external_provider"
    assert hasattr(s, "endpoint_label")
    assert hasattr(s, "vllm_config_ref")
    assert hasattr(s, "concurrency")
    assert hasattr(s, "total_requests")
    assert hasattr(s, "successful_requests")
    assert hasattr(s, "latency_p95_seconds")
    assert hasattr(s, "added_by_task")
    assert hasattr(s, "categories")


def test_full_has_per_percentile_fields(repo: Path) -> None:
    """Cross-task ranking depends on full exposing every percentile / TTFT."""
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )

    full = agg_mod.aggregate_latency_benchmark_runs_full()
    assert len(full) == 1
    f = full[0]
    # Per-percentile latency
    assert f.latency_p50_seconds is not None
    assert f.latency_p95_seconds is not None
    assert f.latency_p99_seconds is not None
    # TTFT
    assert hasattr(f, "ttft_median_seconds")
    assert hasattr(f, "ttft_p95_seconds")
    assert hasattr(f, "ttft_p99_seconds")
    # Tokens / throughput
    assert hasattr(f, "tokens_per_second")
    assert hasattr(f, "input_tokens_total")
    assert hasattr(f, "output_tokens_total")
    # Run metadata
    assert hasattr(f, "start_time_utc")
    assert hasattr(f, "end_time_utc")
    assert hasattr(f, "warmup_seconds")
    assert hasattr(f, "duration_seconds")
    assert hasattr(f, "prompt_dataset_ref")
    assert hasattr(f, "harness")
    assert hasattr(f, "harness_version")


# ---------------------------------------------------------------------------
# vllm_config_ref preservation in full output
# ---------------------------------------------------------------------------


def test_two_runs_with_different_vllm_config_refs_preserve_refs(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_complete_task(repo_root=repo, task_id=TASK_B, task_index=2)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
        endpoint_kind="local_vllm_config",
        vllm_config_ref={"task_id": "t0004_fp8", "config_id": "fp8_v1"},
    )
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_B,
        run_id=RUN_B,
        endpoint_kind="local_vllm_config",
        vllm_config_ref={"task_id": "t0005_int4", "config_id": "int4_v1"},
    )

    full: list[agg_mod.LatencyBenchmarkRunInfoFull] = (
        agg_mod.aggregate_latency_benchmark_runs_full()
    )
    by_id: dict[str, agg_mod.LatencyBenchmarkRunInfoFull] = {f.run_id: f for f in full}
    assert by_id[RUN_A].vllm_config_ref == "t0004_fp8/fp8_v1"
    assert by_id[RUN_B].vllm_config_ref == "t0005_int4/int4_v1"


def test_external_provider_has_null_vllm_config_ref(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
        endpoint_kind="external_provider",
        vllm_config_ref=None,
    )
    short = agg_mod.aggregate_latency_benchmark_runs_short()
    assert short[0].vllm_config_ref is None


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_filter_by_category(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
        categories=[CATEGORY_LATENCY],
    )
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_B,
        categories=[CATEGORY_OTHER],
    )

    result = agg_mod.aggregate_latency_benchmark_runs_short(
        filter_categories=[CATEGORY_LATENCY],
    )
    assert len(result) == 1
    assert result[0].run_id == RUN_A


def test_filter_by_id(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_B,
    )

    result = agg_mod.aggregate_latency_benchmark_runs_short(
        filter_ids=[RUN_B],
    )
    assert len(result) == 1
    assert result[0].run_id == RUN_B


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


def test_json_format(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )
    runs = agg_mod.aggregate_latency_benchmark_runs_short()
    output: str = agg_mod._format_short_json(runs=runs)
    parsed: dict[str, object] = json.loads(output)
    assert parsed["run_count"] == 1
    assert isinstance(parsed["runs"], list)


def test_markdown_format(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )
    runs = agg_mod.aggregate_latency_benchmark_runs_short()
    output: str = agg_mod._format_short_markdown(runs=runs)
    assert RUN_A in output


def test_ids_format(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )
    runs = agg_mod.aggregate_latency_benchmark_runs_short()
    output: str = agg_mod._format_ids(run_ids=[r.run_id for r in runs])
    assert output == RUN_A


def test_full_json_format_includes_percentile_fields(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )
    runs = agg_mod.aggregate_latency_benchmark_runs_full()
    output: str = agg_mod._format_full_json(runs=runs)
    parsed: dict[str, object] = json.loads(output)
    assert parsed["run_count"] == 1
    runs_list: list[dict[str, object]] = parsed["runs"]  # type: ignore[assignment]
    run: dict[str, object] = runs_list[0]
    assert "latency_p50_seconds" in run
    assert "latency_p95_seconds" in run
    assert "latency_p99_seconds" in run
    assert "ttft_median_seconds" in run


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_malformed_details_json_skipped(repo: Path) -> None:
    build_complete_task(repo_root=repo, task_id=TASK_A, task_index=1)
    build_latency_benchmark_run_asset(
        repo_root=repo,
        task_id=TASK_A,
        run_id=RUN_A,
    )

    bad_dir: Path = repo / "tasks" / TASK_A / "assets" / "latency_benchmark_run" / "bad-run"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "details.json").write_text("{invalid", encoding="utf-8")

    result = agg_mod.aggregate_latency_benchmark_runs_short()
    ids: list[str] = [r.run_id for r in result]
    assert RUN_A in ids
    assert "bad-run" not in ids
