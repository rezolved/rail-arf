"""Aggregate all latency-benchmark-run assets in the project.

Discovers run folders under ``tasks/*/assets/latency-benchmark-run/`` (and the
top-level ``assets/latency-benchmark-run/``), loads their ``details.json`` and
``summary.json``, and outputs structured data. Supports filtering by category
and run ID, and short/full detail levels.

Aggregator version: 1
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from arf.scripts.aggregators.common.cli import (
    DETAIL_LEVEL_FULL,
    DETAIL_LEVEL_SHORT,
    OUTPUT_FORMAT_IDS,
    OUTPUT_FORMAT_JSON,
    OUTPUT_FORMAT_MARKDOWN,
    add_detail_level_arg,
    add_filter_args,
    add_output_format_arg,
)
from arf.scripts.aggregators.common.filtering import (
    matches_categories,
    matches_ids,
)
from arf.scripts.verificators.common.paths import (
    TASKS_DIR,
    latency_benchmark_run_base_dir,
    latency_benchmark_run_details_path,
    latency_benchmark_run_summary_path,
)

RUN_COUNT_KEY: str = "run_count"
RUNS_KEY: str = "runs"
EM_DASH: str = "—"

# ---------------------------------------------------------------------------
# Pydantic models (I/O boundary)
# ---------------------------------------------------------------------------


class VllmConfigRefModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    task_id: str
    config_id: str


class LatencyBenchmarkRunDetailsModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    spec_version: str
    run_id: str
    endpoint_kind: str
    endpoint_label: str
    vllm_config_ref: VllmConfigRefModel | None = None
    harness: str
    harness_version: str
    concurrency: int
    duration_seconds: float | None = None
    warmup_seconds: float | None = None
    prompt_dataset_ref: str | None = None
    start_time_utc: str
    end_time_utc: str
    total_requests: int
    successful_requests: int
    summary_path: str
    raw_requests_path: str
    categories: list[str]
    added_by_task: str
    date_added: str


class LatencyBenchmarkRunSummaryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    spec_version: str | None = None
    latency_avg_seconds: float
    latency_p50_seconds: float
    latency_p95_seconds: float
    latency_p99_seconds: float
    ttft_median_seconds: float | None = None
    ttft_p95_seconds: float | None = None
    ttft_p99_seconds: float | None = None
    tokens_per_second: float
    input_tokens_total: int
    output_tokens_total: int


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LatencyBenchmarkRunInfoShort:
    run_id: str
    endpoint_kind: str
    endpoint_label: str
    vllm_config_ref: str | None
    concurrency: int
    total_requests: int
    successful_requests: int
    latency_p95_seconds: float | None
    added_by_task: str
    categories: list[str]


@dataclass(frozen=True, slots=True)
class LatencyBenchmarkRunInfoFull:
    run_id: str
    endpoint_kind: str
    endpoint_label: str
    vllm_config_ref: str | None
    harness: str
    harness_version: str
    concurrency: int
    duration_seconds: float | None
    warmup_seconds: float | None
    prompt_dataset_ref: str | None
    start_time_utc: str
    end_time_utc: str
    total_requests: int
    successful_requests: int
    latency_avg_seconds: float | None
    latency_p50_seconds: float | None
    latency_p95_seconds: float | None
    latency_p99_seconds: float | None
    ttft_median_seconds: float | None
    ttft_p95_seconds: float | None
    ttft_p99_seconds: float | None
    tokens_per_second: float | None
    input_tokens_total: int | None
    output_tokens_total: int | None
    categories: list[str]
    added_by_task: str
    date_added: str


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RunLocation:
    run_id: str
    task_id: str | None


def _discover_runs() -> list[_RunLocation]:
    seen: set[str] = set()
    locations: list[_RunLocation] = []

    if TASKS_DIR.exists():
        for task_dir in sorted(TASKS_DIR.iterdir()):
            if not task_dir.is_dir() or task_dir.name.startswith("."):
                continue
            base: Path = latency_benchmark_run_base_dir(task_id=task_dir.name)
            if not base.exists():
                continue
            for run_dir in sorted(base.iterdir()):
                if (
                    run_dir.is_dir()
                    and not run_dir.name.startswith(".")
                    and run_dir.name not in seen
                ):
                    seen.add(run_dir.name)
                    locations.append(
                        _RunLocation(
                            run_id=run_dir.name,
                            task_id=task_dir.name,
                        ),
                    )

    top_level: Path = latency_benchmark_run_base_dir(task_id=None)
    if top_level.exists():
        for run_dir in sorted(top_level.iterdir()):
            if run_dir.is_dir() and not run_dir.name.startswith(".") and run_dir.name not in seen:
                seen.add(run_dir.name)
                locations.append(
                    _RunLocation(run_id=run_dir.name, task_id=None),
                )

    return locations


def _load_details(
    *,
    run_id: str,
    task_id: str | None,
) -> LatencyBenchmarkRunDetailsModel | None:
    file_path: Path = latency_benchmark_run_details_path(
        run_id=run_id,
        task_id=task_id,
    )
    if not file_path.exists():
        return None
    try:
        raw: str = file_path.read_text(encoding="utf-8")
        return LatencyBenchmarkRunDetailsModel.model_validate_json(raw)
    except (OSError, UnicodeDecodeError, ValidationError):
        return None


def _load_summary(
    *,
    run_id: str,
    task_id: str | None,
    summary_path: str,
) -> LatencyBenchmarkRunSummaryModel | None:
    default_path: Path = latency_benchmark_run_summary_path(
        run_id=run_id,
        task_id=task_id,
    )
    resolved: Path = default_path.parent / summary_path if summary_path else default_path
    if not resolved.exists():
        return None
    try:
        raw: str = resolved.read_text(encoding="utf-8")
        return LatencyBenchmarkRunSummaryModel.model_validate_json(raw)
    except (OSError, UnicodeDecodeError, ValidationError):
        return None


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _format_vllm_ref(ref: VllmConfigRefModel | None) -> str | None:
    if ref is None:
        return None
    return f"{ref.task_id}/{ref.config_id}"


def _to_short(
    *,
    details: LatencyBenchmarkRunDetailsModel,
    summary: LatencyBenchmarkRunSummaryModel | None,
) -> LatencyBenchmarkRunInfoShort:
    latency_p95: float | None = summary.latency_p95_seconds if summary is not None else None
    return LatencyBenchmarkRunInfoShort(
        run_id=details.run_id,
        endpoint_kind=details.endpoint_kind,
        endpoint_label=details.endpoint_label,
        vllm_config_ref=_format_vllm_ref(details.vllm_config_ref),
        concurrency=details.concurrency,
        total_requests=details.total_requests,
        successful_requests=details.successful_requests,
        latency_p95_seconds=latency_p95,
        added_by_task=details.added_by_task,
        categories=details.categories,
    )


def _to_full(
    *,
    details: LatencyBenchmarkRunDetailsModel,
    summary: LatencyBenchmarkRunSummaryModel | None,
) -> LatencyBenchmarkRunInfoFull:
    return LatencyBenchmarkRunInfoFull(
        run_id=details.run_id,
        endpoint_kind=details.endpoint_kind,
        endpoint_label=details.endpoint_label,
        vllm_config_ref=_format_vllm_ref(details.vllm_config_ref),
        harness=details.harness,
        harness_version=details.harness_version,
        concurrency=details.concurrency,
        duration_seconds=details.duration_seconds,
        warmup_seconds=details.warmup_seconds,
        prompt_dataset_ref=details.prompt_dataset_ref,
        start_time_utc=details.start_time_utc,
        end_time_utc=details.end_time_utc,
        total_requests=details.total_requests,
        successful_requests=details.successful_requests,
        latency_avg_seconds=summary.latency_avg_seconds if summary is not None else None,
        latency_p50_seconds=summary.latency_p50_seconds if summary is not None else None,
        latency_p95_seconds=summary.latency_p95_seconds if summary is not None else None,
        latency_p99_seconds=summary.latency_p99_seconds if summary is not None else None,
        ttft_median_seconds=summary.ttft_median_seconds if summary is not None else None,
        ttft_p95_seconds=summary.ttft_p95_seconds if summary is not None else None,
        ttft_p99_seconds=summary.ttft_p99_seconds if summary is not None else None,
        tokens_per_second=summary.tokens_per_second if summary is not None else None,
        input_tokens_total=summary.input_tokens_total if summary is not None else None,
        output_tokens_total=summary.output_tokens_total if summary is not None else None,
        categories=details.categories,
        added_by_task=details.added_by_task,
        date_added=details.date_added,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_latency_benchmark_runs_short(
    *,
    filter_categories: list[str] | None = None,
    filter_ids: list[str] | None = None,
) -> list[LatencyBenchmarkRunInfoShort]:
    runs: list[LatencyBenchmarkRunInfoShort] = []
    for loc in _discover_runs():
        if not matches_ids(asset_id=loc.run_id, filter_ids=filter_ids):
            continue
        details: LatencyBenchmarkRunDetailsModel | None = _load_details(
            run_id=loc.run_id,
            task_id=loc.task_id,
        )
        if details is None:
            continue
        if not matches_categories(
            asset_categories=details.categories,
            filter_categories=filter_categories,
        ):
            continue
        summary: LatencyBenchmarkRunSummaryModel | None = _load_summary(
            run_id=loc.run_id,
            task_id=loc.task_id,
            summary_path=details.summary_path,
        )
        runs.append(_to_short(details=details, summary=summary))
    return runs


def aggregate_latency_benchmark_runs_full(
    *,
    filter_categories: list[str] | None = None,
    filter_ids: list[str] | None = None,
) -> list[LatencyBenchmarkRunInfoFull]:
    runs: list[LatencyBenchmarkRunInfoFull] = []
    for loc in _discover_runs():
        if not matches_ids(asset_id=loc.run_id, filter_ids=filter_ids):
            continue
        details: LatencyBenchmarkRunDetailsModel | None = _load_details(
            run_id=loc.run_id,
            task_id=loc.task_id,
        )
        if details is None:
            continue
        if not matches_categories(
            asset_categories=details.categories,
            filter_categories=filter_categories,
        ):
            continue
        summary: LatencyBenchmarkRunSummaryModel | None = _load_summary(
            run_id=loc.run_id,
            task_id=loc.task_id,
            summary_path=details.summary_path,
        )
        runs.append(_to_full(details=details, summary=summary))
    return runs


# ---------------------------------------------------------------------------
# Output formatting — short
# ---------------------------------------------------------------------------


def _format_short_json(*, runs: list[LatencyBenchmarkRunInfoShort]) -> str:
    records: list[dict[str, Any]] = [asdict(r) for r in runs]
    output: dict[str, Any] = {
        RUN_COUNT_KEY: len(records),
        RUNS_KEY: records,
    }
    return json.dumps(obj=output, indent=2, ensure_ascii=False)


def _format_short_markdown(*, runs: list[LatencyBenchmarkRunInfoShort]) -> str:
    if len(runs) == 0:
        return "No latency-benchmark-run assets found."
    lines: list[str] = [f"# Latency Benchmark Runs ({len(runs)})", ""]
    lines.append(
        "| Run ID | Endpoint Kind | Endpoint Label | Config Ref | "
        "Concurrency | Total | Successful | p95 (s) | Task |",
    )
    lines.append(
        "|--------|---------------|----------------|------------|"
        "-------------|-------|------------|---------|------|"
    )
    for r in runs:
        ref_str: str = r.vllm_config_ref if r.vllm_config_ref is not None else EM_DASH
        p95_str: str = (
            f"{r.latency_p95_seconds:.3f}" if r.latency_p95_seconds is not None else EM_DASH
        )
        lines.append(
            f"| `{r.run_id}` | {r.endpoint_kind} | {r.endpoint_label}"
            f" | {ref_str} | {r.concurrency} | {r.total_requests}"
            f" | {r.successful_requests} | {p95_str} | `{r.added_by_task}` |",
        )
    return "\n".join(lines)


def _format_ids(*, run_ids: list[str]) -> str:
    return "\n".join(run_ids)


# ---------------------------------------------------------------------------
# Output formatting — full
# ---------------------------------------------------------------------------


def _format_full_json(*, runs: list[LatencyBenchmarkRunInfoFull]) -> str:
    records: list[dict[str, Any]] = [asdict(r) for r in runs]
    output: dict[str, Any] = {
        RUN_COUNT_KEY: len(records),
        RUNS_KEY: records,
    }
    return json.dumps(obj=output, indent=2, ensure_ascii=False)


def _format_full_markdown(*, runs: list[LatencyBenchmarkRunInfoFull]) -> str:
    if len(runs) == 0:
        return "No latency-benchmark-run assets found."
    lines: list[str] = [f"# Latency Benchmark Runs ({len(runs)})", ""]
    for r in runs:
        categories_str: str = (
            ", ".join(f"`{c}`" for c in r.categories) if len(r.categories) > 0 else EM_DASH
        )
        ref_str: str = r.vllm_config_ref if r.vllm_config_ref is not None else EM_DASH
        prompt_str: str = r.prompt_dataset_ref if r.prompt_dataset_ref is not None else EM_DASH
        lines.append(f"## {r.run_id}")
        lines.append("")
        lines.append(f"* **Endpoint kind**: `{r.endpoint_kind}`")
        lines.append(f"* **Endpoint label**: `{r.endpoint_label}`")
        lines.append(f"* **vLLM config ref**: {ref_str}")
        lines.append(f"* **Harness**: `{r.harness}` (version `{r.harness_version}`)")
        lines.append(f"* **Concurrency**: {r.concurrency}")
        if r.duration_seconds is not None:
            lines.append(f"* **Duration (s)**: {r.duration_seconds}")
        if r.warmup_seconds is not None:
            lines.append(f"* **Warmup (s)**: {r.warmup_seconds}")
        lines.append(f"* **Prompt dataset**: {prompt_str}")
        lines.append(f"* **Window**: {r.start_time_utc} -> {r.end_time_utc}")
        lines.append(
            f"* **Requests**: {r.successful_requests}/{r.total_requests} successful",
        )
        if r.latency_p50_seconds is not None:
            lines.append(
                f"* **Latency (s)**: p50={r.latency_p50_seconds}"
                f" p95={r.latency_p95_seconds} p99={r.latency_p99_seconds}"
                f" avg={r.latency_avg_seconds}",
            )
        if r.ttft_median_seconds is not None:
            lines.append(
                f"* **TTFT (s)**: median={r.ttft_median_seconds}"
                f" p95={r.ttft_p95_seconds} p99={r.ttft_p99_seconds}",
            )
        if r.tokens_per_second is not None:
            lines.append(f"* **Tokens/sec**: {r.tokens_per_second}")
        if r.input_tokens_total is not None:
            lines.append(
                f"* **Tokens**: input={r.input_tokens_total} output={r.output_tokens_total}",
            )
        lines.append(f"* **Categories**: {categories_str}")
        lines.append(f"* **Added by**: `{r.added_by_task}` on {r.date_added}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Aggregate all latency-benchmark-run assets in the project",
    )
    add_output_format_arg(parser=parser)
    add_detail_level_arg(parser=parser)
    add_filter_args(parser=parser)
    args: argparse.Namespace = parser.parse_args()

    output_format: str = args.format
    detail_level: str = args.detail
    filter_categories: list[str] | None = args.categories
    filter_ids: list[str] | None = args.ids

    if detail_level == DETAIL_LEVEL_SHORT:
        runs_short: list[LatencyBenchmarkRunInfoShort] = aggregate_latency_benchmark_runs_short(
            filter_categories=filter_categories,
            filter_ids=filter_ids,
        )
        if output_format == OUTPUT_FORMAT_JSON:
            print(_format_short_json(runs=runs_short))
        elif output_format == OUTPUT_FORMAT_MARKDOWN:
            print(_format_short_markdown(runs=runs_short))
        elif output_format == OUTPUT_FORMAT_IDS:
            print(_format_ids(run_ids=[r.run_id for r in runs_short]))
        else:
            print(f"Unknown format: {output_format}", file=sys.stderr)
            sys.exit(1)
    elif detail_level == DETAIL_LEVEL_FULL:
        runs_full: list[LatencyBenchmarkRunInfoFull] = aggregate_latency_benchmark_runs_full(
            filter_categories=filter_categories,
            filter_ids=filter_ids,
        )
        if output_format == OUTPUT_FORMAT_JSON:
            print(_format_full_json(runs=runs_full))
        elif output_format == OUTPUT_FORMAT_MARKDOWN:
            print(_format_full_markdown(runs=runs_full))
        elif output_format == OUTPUT_FORMAT_IDS:
            print(_format_ids(run_ids=[r.run_id for r in runs_full]))
        else:
            print(f"Unknown format: {output_format}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown detail level: {detail_level}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
