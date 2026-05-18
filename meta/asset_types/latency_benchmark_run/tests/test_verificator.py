"""Tests for the latency_benchmark_run verificator."""

import json
from pathlib import Path

import pytest

import arf.tests.fixtures.asset_builders.latency_benchmark_run as lbr_builder_module
import meta.asset_types.latency_benchmark_run.verificator as verificator_module
import meta.asset_types.latency_benchmark_run.verify_details as verify_details_module
import meta.asset_types.latency_benchmark_run.verify_summary as verify_summary_module
from arf.scripts.verificators.common.paths import (
    latency_benchmark_run_asset_dir,
    latency_benchmark_run_details_path,
    latency_benchmark_run_raw_requests_path,
    latency_benchmark_run_summary_path,
    vllm_config_asset_dir,
)
from arf.scripts.verificators.common.types import VerificationResult
from arf.tests.fixtures.asset_builders.latency_benchmark_run import (
    DEFAULT_RUN_ID,
    DEFAULT_TASK_ID,
    build_latency_benchmark_run_asset,
)
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.task_builder import build_complete_task
from arf.tests.fixtures.writers import write_json, write_text

_VERIFICATOR_MODULES = [
    verificator_module,
    verify_details_module,
    verify_summary_module,
    lbr_builder_module,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diagnostic_codes(result: VerificationResult) -> list[str]:
    return [d.code.text for d in result.diagnostics]


def _error_codes(result: VerificationResult) -> list[str]:
    return [d.code.text for d in result.errors]


def _warning_codes(result: VerificationResult) -> list[str]:
    return [d.code.text for d in result.warnings]


def _verify(*, run_id: str = DEFAULT_RUN_ID, task_id: str = DEFAULT_TASK_ID) -> VerificationResult:
    return verificator_module.verify_latency_benchmark_run_asset(
        run_id=run_id,
        task_id=task_id,
    )


def _write_vllm_config_stub(*, repo_root: Path, config_id: str, task_id: str) -> None:
    del repo_root
    asset_dir: Path = vllm_config_asset_dir(config_id=config_id, task_id=task_id)
    asset_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=tmp_path,
        verificator_modules=_VERIFICATOR_MODULES,
    )
    build_complete_task(repo_root=tmp_path, task_id=DEFAULT_TASK_ID)
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_run_passes(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    result: VerificationResult = _verify()
    assert result.passed is True, _diagnostic_codes(result)


def test_valid_local_vllm_config_run_passes(repo: Path) -> None:
    _write_vllm_config_stub(
        repo_root=repo,
        config_id="vllm_fp8_v1",
        task_id="t0004_vllm_fp8",
    )
    build_latency_benchmark_run_asset(
        repo_root=repo,
        endpoint_kind="local_vllm_config",
        vllm_config_ref={
            "task_id": "t0004_vllm_fp8",
            "config_id": "vllm_fp8_v1",
        },
    )
    result: VerificationResult = _verify()
    assert "LBR-E007" not in _error_codes(result)
    assert "LBR-E008" not in _error_codes(result)
    assert "LBR-E012" not in _error_codes(result)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_e001_details_json_missing(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    latency_benchmark_run_details_path(run_id=DEFAULT_RUN_ID, task_id=DEFAULT_TASK_ID).unlink()
    result: VerificationResult = _verify()
    assert "LBR-E001" in _error_codes(result)


def test_e001_details_json_invalid(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    write_text(
        path=latency_benchmark_run_details_path(
            run_id=DEFAULT_RUN_ID,
            task_id=DEFAULT_TASK_ID,
        ),
        content="{not valid json",
    )
    result: VerificationResult = _verify()
    assert "LBR-E001" in _error_codes(result)


def test_e002_summary_json_missing(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo, include_summary=False)
    result: VerificationResult = _verify()
    assert "LBR-E002" in _error_codes(result)


def test_e002_summary_json_invalid(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    write_text(
        path=latency_benchmark_run_summary_path(
            run_id=DEFAULT_RUN_ID,
            task_id=DEFAULT_TASK_ID,
        ),
        content="not valid",
    )
    result: VerificationResult = _verify()
    assert "LBR-E002" in _error_codes(result)


def test_e003_raw_requests_missing(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo, include_raw_requests=False)
    result: VerificationResult = _verify()
    assert "LBR-E003" in _error_codes(result)


def test_e004_run_id_mismatch(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        details_overrides={"run_id": "different_run_id"},
    )
    result: VerificationResult = _verify()
    assert "LBR-E004" in _error_codes(result)


def test_e005_required_field_missing(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    details_path: Path = latency_benchmark_run_details_path(
        run_id=DEFAULT_RUN_ID,
        task_id=DEFAULT_TASK_ID,
    )
    data: dict[str, object] = json.loads(details_path.read_text(encoding="utf-8"))
    del data["harness"]
    write_json(path=details_path, data=data)
    result: VerificationResult = _verify()
    assert "LBR-E005" in _error_codes(result)


def test_e006_spec_version_missing_details(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    details_path: Path = latency_benchmark_run_details_path(
        run_id=DEFAULT_RUN_ID,
        task_id=DEFAULT_TASK_ID,
    )
    data: dict[str, object] = json.loads(details_path.read_text(encoding="utf-8"))
    del data["spec_version"]
    write_json(path=details_path, data=data)
    result: VerificationResult = _verify()
    assert "LBR-E006" in _error_codes(result)


def test_e006_spec_version_missing_summary(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    summary_path: Path = latency_benchmark_run_summary_path(
        run_id=DEFAULT_RUN_ID,
        task_id=DEFAULT_TASK_ID,
    )
    data: dict[str, object] = json.loads(summary_path.read_text(encoding="utf-8"))
    del data["spec_version"]
    write_json(path=summary_path, data=data)
    result: VerificationResult = _verify()
    assert "LBR-E006" in _error_codes(result)


def test_e007_local_vllm_with_null_ref(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        endpoint_kind="local_vllm_config",
        vllm_config_ref=None,
    )
    result: VerificationResult = _verify()
    assert "LBR-E007" in _error_codes(result)


def test_e007_local_vllm_with_missing_field(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        endpoint_kind="local_vllm_config",
        vllm_config_ref={"task_id": "t0004_vllm_fp8"},  # missing config_id
    )
    result: VerificationResult = _verify()
    assert "LBR-E007" in _error_codes(result)


def test_e008_external_provider_with_non_null_ref(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        endpoint_kind="external_provider",
        vllm_config_ref={
            "task_id": "t0004_vllm_fp8",
            "config_id": "vllm_fp8_v1",
        },
    )
    result: VerificationResult = _verify()
    assert "LBR-E008" in _error_codes(result)


def test_e009_invalid_endpoint_kind(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        endpoint_kind="something_else",
    )
    result: VerificationResult = _verify()
    assert "LBR-E009" in _error_codes(result)


def test_e010_latency_percentiles_not_monotonic(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        summary_overrides={
            "latency_p50_seconds": 5.0,
            "latency_p95_seconds": 3.0,
            "latency_p99_seconds": 4.0,
        },
    )
    result: VerificationResult = _verify()
    assert "LBR-E010" in _error_codes(result)


def test_e011_successful_exceeds_total(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        total_requests=4,
        successful_requests=10,
    )
    result: VerificationResult = _verify()
    assert "LBR-E011" in _error_codes(result)


def test_e012_vllm_config_ref_nonexistent(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        endpoint_kind="local_vllm_config",
        vllm_config_ref={
            "task_id": "t9999_nonexistent",
            "config_id": "not_real",
        },
    )
    result: VerificationResult = _verify()
    assert "LBR-E012" in _error_codes(result)


def test_e013_concurrency_not_positive(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        concurrency=0,
    )
    result: VerificationResult = _verify()
    assert "LBR-E013" in _error_codes(result)


def test_e014_end_before_start(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        start_time_utc="2026-05-20T14:40:00Z",
        end_time_utc="2026-05-20T14:30:00Z",
    )
    result: VerificationResult = _verify()
    assert "LBR-E014" in _error_codes(result)


def test_e015_summary_required_field_missing(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    summary_path: Path = latency_benchmark_run_summary_path(
        run_id=DEFAULT_RUN_ID,
        task_id=DEFAULT_TASK_ID,
    )
    data: dict[str, object] = json.loads(summary_path.read_text(encoding="utf-8"))
    del data["tokens_per_second"]
    write_json(path=summary_path, data=data)
    result: VerificationResult = _verify()
    assert "LBR-E015" in _error_codes(result)


def test_e015_summary_wrong_type(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        summary_overrides={"latency_avg_seconds": "not a number"},
    )
    result: VerificationResult = _verify()
    assert "LBR-E015" in _error_codes(result)


def test_e016_ttft_percentiles_not_monotonic(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        summary_overrides={
            "ttft_median_seconds": 0.9,
            "ttft_p95_seconds": 0.5,
            "ttft_p99_seconds": 0.8,
        },
    )
    result: VerificationResult = _verify()
    assert "LBR-E016" in _error_codes(result)


def test_e016_ttft_inconsistent_null(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        summary_overrides={
            "ttft_median_seconds": None,
            "ttft_p95_seconds": 0.5,
            "ttft_p99_seconds": 0.8,
        },
    )
    result: VerificationResult = _verify()
    assert "LBR-E016" in _error_codes(result)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def test_w001_line_count_mismatch(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        total_requests=5,
        successful_requests=5,
        raw_request_count=3,
    )
    result: VerificationResult = _verify()
    assert "LBR-W001" in _warning_codes(result)


def test_w002_ttft_null_with_output_tokens(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        summary_overrides={
            "ttft_median_seconds": None,
            "ttft_p95_seconds": None,
            "ttft_p99_seconds": None,
            "output_tokens_total": 4096,
        },
    )
    result: VerificationResult = _verify()
    assert "LBR-W002" in _warning_codes(result)


def test_w003_warmup_null(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        warmup_seconds=None,
    )
    result: VerificationResult = _verify()
    assert "LBR-W003" in _warning_codes(result)


def test_w004_nonexistent_category(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        categories=["does-not-exist"],
    )
    result: VerificationResult = _verify()
    assert "LBR-W004" in _warning_codes(result)


def test_w005_low_success_rate(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        total_requests=100,
        successful_requests=80,
        raw_request_count=100,
    )
    result: VerificationResult = _verify()
    assert "LBR-W005" in _warning_codes(result)


def test_w006_prompt_dataset_ref_null(repo: Path) -> None:
    build_latency_benchmark_run_asset(
        repo_root=repo,
        prompt_dataset_ref=None,
    )
    result: VerificationResult = _verify()
    assert "LBR-W006" in _warning_codes(result)


def test_w007_malformed_jsonl_line(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    raw_path: Path = latency_benchmark_run_raw_requests_path(
        run_id=DEFAULT_RUN_ID,
        task_id=DEFAULT_TASK_ID,
    )
    content: str = raw_path.read_text(encoding="utf-8")
    # Append a malformed line
    write_text(path=raw_path, content=content + "{not json\n")
    result: VerificationResult = _verify()
    assert "LBR-W007" in _warning_codes(result)


# ---------------------------------------------------------------------------
# Smoke: asset_dir exists
# ---------------------------------------------------------------------------


def test_result_file_path_points_at_asset_dir(repo: Path) -> None:
    build_latency_benchmark_run_asset(repo_root=repo)
    result: VerificationResult = _verify()
    assert result.file_path == latency_benchmark_run_asset_dir(
        run_id=DEFAULT_RUN_ID,
        task_id=DEFAULT_TASK_ID,
    )
