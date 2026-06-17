"""Builder fixture for latency-benchmark-run assets used in tests."""

import json
from pathlib import Path

from arf.scripts.verificators.common.paths import (
    latency_benchmark_run_asset_dir,
    latency_benchmark_run_details_path,
    latency_benchmark_run_raw_requests_path,
    latency_benchmark_run_summary_path,
)
from arf.tests.fixtures.writers import write_json, write_text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPEC_VERSION_FIELD: str = "spec_version"
RUN_ID_FIELD: str = "run_id"
ENDPOINT_KIND_FIELD: str = "endpoint_kind"
ENDPOINT_LABEL_FIELD: str = "endpoint_label"
VLLM_CONFIG_REF_FIELD: str = "vllm_config_ref"
HARNESS_FIELD: str = "harness"
HARNESS_VERSION_FIELD: str = "harness_version"
CONCURRENCY_FIELD: str = "concurrency"
DURATION_SECONDS_FIELD: str = "duration_seconds"
WARMUP_SECONDS_FIELD: str = "warmup_seconds"
PROMPT_DATASET_REF_FIELD: str = "prompt_dataset_ref"
START_TIME_FIELD: str = "start_time_utc"
END_TIME_FIELD: str = "end_time_utc"
TOTAL_REQUESTS_FIELD: str = "total_requests"
SUCCESSFUL_REQUESTS_FIELD: str = "successful_requests"
SUMMARY_PATH_FIELD: str = "summary_path"
RAW_REQUESTS_PATH_FIELD: str = "raw_requests_path"
CATEGORIES_FIELD: str = "categories"
ADDED_BY_TASK_FIELD: str = "added_by_task"
DATE_ADDED_FIELD: str = "date_added"

DEFAULT_SPEC_VERSION: str = "1"
DEFAULT_RUN_ID: str = "nebius_fast_20260520_1430_a"
DEFAULT_TASK_ID: str = "t0001_test"
DEFAULT_ENDPOINT_KIND: str = "external_provider"
DEFAULT_ENDPOINT_LABEL: str = "nebius_fast"
DEFAULT_HARNESS: str = "vorontsov-latency-harness"
DEFAULT_HARNESS_VERSION: str = "0.4.1"
DEFAULT_CONCURRENCY: int = 16
DEFAULT_DURATION_SECONDS: float = 600.0
DEFAULT_WARMUP_SECONDS: float = 60.0
DEFAULT_PROMPT_DATASET_REF: str = "rail_internal_toolcalling_eval_v2"
DEFAULT_START_TIME: str = "2026-05-20T14:30:00Z"
DEFAULT_END_TIME: str = "2026-05-20T14:40:00Z"
DEFAULT_TOTAL_REQUESTS: int = 4
DEFAULT_SUCCESSFUL_REQUESTS: int = 4
DEFAULT_SUMMARY_PATH: str = "summary.json"
DEFAULT_RAW_REQUESTS_PATH: str = "raw_requests.jsonl"
DEFAULT_DATE_ADDED: str = "2026-05-20"

DEFAULT_SUMMARY: dict[str, object] = {
    "spec_version": "1",
    "latency_avg_seconds": 1.842,
    "latency_p50_seconds": 1.611,
    "latency_p95_seconds": 3.214,
    "latency_p99_seconds": 4.875,
    "ttft_median_seconds": 0.184,
    "ttft_p95_seconds": 0.523,
    "ttft_p99_seconds": 0.812,
    "tokens_per_second": 73.4,
    "input_tokens_total": 4096,
    "output_tokens_total": 2048,
}


def _default_raw_request_line(*, index: int) -> dict[str, object]:
    return {
        "prompt_id": f"prompt_{index:04d}",
        "submit_time": "2026-05-20T14:30:05Z",
        "first_token_time": "2026-05-20T14:30:05Z",
        "completion_time": "2026-05-20T14:30:07Z",
        "input_token_count": 1024,
        "output_token_count": 512,
        "status": "success",
    }


def build_latency_benchmark_run_asset(
    *,
    repo_root: Path,
    run_id: str = DEFAULT_RUN_ID,
    task_id: str = DEFAULT_TASK_ID,
    spec_version: str = DEFAULT_SPEC_VERSION,
    endpoint_kind: str = DEFAULT_ENDPOINT_KIND,
    endpoint_label: str = DEFAULT_ENDPOINT_LABEL,
    vllm_config_ref: dict[str, str] | None = None,
    harness: str = DEFAULT_HARNESS,
    harness_version: str = DEFAULT_HARNESS_VERSION,
    concurrency: int = DEFAULT_CONCURRENCY,
    duration_seconds: float | None = DEFAULT_DURATION_SECONDS,
    warmup_seconds: float | None = DEFAULT_WARMUP_SECONDS,
    prompt_dataset_ref: str | None = DEFAULT_PROMPT_DATASET_REF,
    start_time_utc: str = DEFAULT_START_TIME,
    end_time_utc: str = DEFAULT_END_TIME,
    total_requests: int = DEFAULT_TOTAL_REQUESTS,
    successful_requests: int = DEFAULT_SUCCESSFUL_REQUESTS,
    summary_path: str = DEFAULT_SUMMARY_PATH,
    raw_requests_path: str = DEFAULT_RAW_REQUESTS_PATH,
    categories: list[str] | None = None,
    added_by_task: str | None = None,
    date_added: str = DEFAULT_DATE_ADDED,
    include_summary: bool = True,
    include_raw_requests: bool = True,
    details_overrides: dict[str, object] | None = None,
    summary_overrides: dict[str, object] | None = None,
    raw_request_count: int | None = None,
) -> Path:
    del repo_root

    asset_dir: Path = latency_benchmark_run_asset_dir(
        run_id=run_id,
        task_id=task_id,
    )
    asset_dir.mkdir(parents=True, exist_ok=True)

    resolved_categories: list[str] = categories if categories is not None else []
    resolved_added_by: str = added_by_task if added_by_task is not None else task_id

    details: dict[str, object] = {
        SPEC_VERSION_FIELD: spec_version,
        RUN_ID_FIELD: run_id,
        ENDPOINT_KIND_FIELD: endpoint_kind,
        ENDPOINT_LABEL_FIELD: endpoint_label,
        VLLM_CONFIG_REF_FIELD: vllm_config_ref,
        HARNESS_FIELD: harness,
        HARNESS_VERSION_FIELD: harness_version,
        CONCURRENCY_FIELD: concurrency,
        DURATION_SECONDS_FIELD: duration_seconds,
        WARMUP_SECONDS_FIELD: warmup_seconds,
        PROMPT_DATASET_REF_FIELD: prompt_dataset_ref,
        START_TIME_FIELD: start_time_utc,
        END_TIME_FIELD: end_time_utc,
        TOTAL_REQUESTS_FIELD: total_requests,
        SUCCESSFUL_REQUESTS_FIELD: successful_requests,
        SUMMARY_PATH_FIELD: summary_path,
        RAW_REQUESTS_PATH_FIELD: raw_requests_path,
        CATEGORIES_FIELD: resolved_categories,
        ADDED_BY_TASK_FIELD: resolved_added_by,
        DATE_ADDED_FIELD: date_added,
    }

    if details_overrides is not None:
        details.update(details_overrides)

    write_json(
        path=latency_benchmark_run_details_path(
            run_id=run_id,
            task_id=task_id,
        ),
        data=details,
    )

    if include_summary:
        summary: dict[str, object] = dict(DEFAULT_SUMMARY)
        if summary_overrides is not None:
            summary.update(summary_overrides)
        write_json(
            path=latency_benchmark_run_summary_path(
                run_id=run_id,
                task_id=task_id,
            ),
            data=summary,
        )

    if include_raw_requests:
        line_count: int = raw_request_count if raw_request_count is not None else total_requests
        lines: list[str] = [
            json.dumps(_default_raw_request_line(index=i)) for i in range(line_count)
        ]
        write_text(
            path=latency_benchmark_run_raw_requests_path(
                run_id=run_id,
                task_id=task_id,
            ),
            content="\n".join(lines) + ("\n" if len(lines) > 0 else ""),
        )

    return asset_dir
