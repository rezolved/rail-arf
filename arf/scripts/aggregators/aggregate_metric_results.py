"""Aggregate metric values across all tasks.

Walks task folders, reads results/metrics.json, normalizes both
legacy flat and explicit variant formats, applies the `metrics`
corrections overlay, and groups the effective metric values by
metric key.

Aggregator version: 3
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any

from arf.scripts.aggregators.aggregate_metrics import (
    MetricInfoFull,
    aggregate_metrics_full,
)
from arf.scripts.aggregators.common.cli import (
    DETAIL_LEVEL_FULL,
    DETAIL_LEVEL_SHORT,
    OUTPUT_FORMAT_JSON,
    OUTPUT_FORMAT_MARKDOWN,
    add_detail_level_arg,
    add_output_format_arg,
)
from arf.scripts.aggregators.common.filtering import matches_ids
from arf.scripts.common.artifacts import (
    METRICS_PAYLOAD_FIELD_VALUE,
    METRICS_PAYLOAD_FIELD_VARIANT_LABEL,
    TARGET_KIND_METRICS,
    TargetKey,
)
from arf.scripts.common.corrections import (
    EffectiveTargetRecord,
    build_correction_index,
    dedupe_effective_records,
    discover_corrections,
    load_effective_target_record,
    resolve_target,
)
from arf.scripts.common.task_metrics import (
    MetricsTargetId,
    TaskMetricsDocument,
    TaskMetricsFormatError,
    build_metrics_target_id,
    normalize_task_metrics_data,
    parse_metrics_target_id,
)
from arf.scripts.verificators.common.json_utils import load_json_file
from arf.scripts.verificators.common.paths import TASKS_DIR, metrics_path

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricResultEntry:
    task_id: str
    variant_id: str
    variant_label: str | None
    value: float | int | bool | str | None


@dataclass(frozen=True, slots=True)
class MetricResultsShort:
    metric_key: str
    result_count: int
    entries: list[MetricResultEntry]


@dataclass(frozen=True, slots=True)
class MetricResultsFull:
    metric_key: str
    metric_name: str
    unit: str
    value_type: str
    higher_is_better: bool
    is_key: bool
    emoji: str | None
    result_count: int
    entries: list[MetricResultEntry]


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _discover_task_ids() -> list[str]:
    if not TASKS_DIR.exists():
        return []
    return sorted(
        d.name
        for d in TASKS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("__")
    )


def _discover_raw_metric_target_keys() -> list[TargetKey]:
    keys: list[TargetKey] = []
    for task_id in _discover_task_ids():
        file_path = metrics_path(task_id=task_id)
        data: dict[str, Any] | None = load_json_file(file_path=file_path)
        if data is None:
            continue
        try:
            document: TaskMetricsDocument = normalize_task_metrics_data(data=data)
        except TaskMetricsFormatError:
            continue
        for variant in document.variants:
            for metric_key in variant.metrics:
                keys.append(
                    TargetKey(
                        task_id=task_id,
                        target_kind=TARGET_KIND_METRICS,
                        target_id=build_metrics_target_id(
                            metric_key=metric_key,
                            variant_id=variant.variant_id,
                        ),
                    ),
                )
    return keys


def _entry_from_effective_record(
    *,
    record: EffectiveTargetRecord,
    variant_id: str,
) -> MetricResultEntry:
    type _ScalarValue = float | int | bool | str | None

    raw_value: object = record.payload.get(METRICS_PAYLOAD_FIELD_VALUE)
    value: _ScalarValue
    if isinstance(raw_value, float | int | bool | str) or raw_value is None:
        value = raw_value
    else:
        value = None

    raw_label: object = record.payload.get(METRICS_PAYLOAD_FIELD_VARIANT_LABEL)
    variant_label: str | None = raw_label if isinstance(raw_label, str) else None

    return MetricResultEntry(
        task_id=record.effective_key.task_id,
        variant_id=variant_id,
        variant_label=variant_label,
        value=value,
    )


def _collect_all_entries(
    *,
    filter_task_ids: list[str] | None = None,
) -> dict[str, list[MetricResultEntry]]:
    correction_index = build_correction_index(
        correction_specs=discover_corrections(),
    )
    effective_records: list[EffectiveTargetRecord] = []
    for original_key in _discover_raw_metric_target_keys():
        if not matches_ids(asset_id=original_key.task_id, filter_ids=filter_task_ids):
            continue
        resolution = resolve_target(
            original_key=original_key,
            correction_index=correction_index,
        )
        if resolution.deleted:
            continue
        record: EffectiveTargetRecord | None = load_effective_target_record(
            resolution=resolution,
            correction_index=correction_index,
        )
        if record is not None:
            effective_records.append(record)
    deduped_records: list[EffectiveTargetRecord] = dedupe_effective_records(
        records=effective_records,
    )

    grouped: dict[str, list[MetricResultEntry]] = {}
    for record in deduped_records:
        parsed_target_id: MetricsTargetId | None = parse_metrics_target_id(
            target_id=record.effective_key.target_id,
        )
        if parsed_target_id is None:
            continue
        if parsed_target_id.metric_key not in grouped:
            grouped[parsed_target_id.metric_key] = []
        grouped[parsed_target_id.metric_key].append(
            _entry_from_effective_record(
                record=record,
                variant_id=parsed_target_id.variant_id,
            ),
        )
    return grouped


def aggregate_metric_results_short(
    *,
    filter_metric_keys: list[str] | None = None,
    filter_task_ids: list[str] | None = None,
) -> list[MetricResultsShort]:
    grouped: dict[str, list[MetricResultEntry]] = _collect_all_entries(
        filter_task_ids=filter_task_ids,
    )
    results: list[MetricResultsShort] = []
    for metric_key in sorted(grouped.keys()):
        if not matches_ids(
            asset_id=metric_key,
            filter_ids=filter_metric_keys,
        ):
            continue
        entries: list[MetricResultEntry] = grouped[metric_key]
        results.append(
            MetricResultsShort(
                metric_key=metric_key,
                result_count=len(entries),
                entries=entries,
            ),
        )
    return results


def aggregate_metric_results_full(
    *,
    filter_metric_keys: list[str] | None = None,
    filter_task_ids: list[str] | None = None,
) -> list[MetricResultsFull]:
    grouped: dict[str, list[MetricResultEntry]] = _collect_all_entries(
        filter_task_ids=filter_task_ids,
    )
    metric_defs: list[MetricInfoFull] = aggregate_metrics_full()
    def_map: dict[str, MetricInfoFull] = {m.metric_key: m for m in metric_defs}

    results: list[MetricResultsFull] = []
    for metric_key in sorted(grouped.keys()):
        if not matches_ids(
            asset_id=metric_key,
            filter_ids=filter_metric_keys,
        ):
            continue
        entries: list[MetricResultEntry] = grouped[metric_key]
        definition: MetricInfoFull | None = def_map.get(metric_key)
        results.append(
            MetricResultsFull(
                metric_key=metric_key,
                metric_name=(definition.name if definition is not None else metric_key),
                unit=definition.unit if definition is not None else "none",
                value_type=(definition.value_type if definition is not None else "float"),
                higher_is_better=(definition.higher_is_better if definition is not None else True),
                is_key=(definition.is_key if definition is not None else False),
                emoji=(definition.emoji if definition is not None else None),
                result_count=len(entries),
                entries=entries,
            ),
        )
    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_short_json(
    *,
    results: list[MetricResultsShort],
) -> str:
    records: list[dict[str, Any]] = [asdict(r) for r in results]
    output: dict[str, Any] = {
        "metric_count": len(records),
        "metric_results": records,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def _format_full_json(
    *,
    results: list[MetricResultsFull],
) -> str:
    records: list[dict[str, Any]] = [asdict(r) for r in results]
    output: dict[str, Any] = {
        "metric_count": len(records),
        "metric_results": records,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def _sort_key_for_value(*, value: object) -> tuple[int, float]:
    if isinstance(value, bool):
        return (1, -float(value))
    if isinstance(value, int | float):
        return (0, -float(value))
    if value is None:
        return (2, 0.0)
    return (1, 0.0)


def _format_full_markdown(
    *,
    results: list[MetricResultsFull],
) -> str:
    if len(results) == 0:
        return "No metric results found."

    lines: list[str] = [
        f"# Metric Results ({len(results)} metrics)",
        "",
    ]

    for r in results:
        emoji_prefix: str = f"{r.emoji} " if r.emoji is not None else ""
        lines.append(
            f"## {emoji_prefix}{r.metric_name} ({r.result_count} results) {{#{r.metric_key}}}",
        )
        lines.append("")
        if r.result_count == 0:
            lines.append("No results.")
            lines.append("")
            continue

        sorted_entries: list[MetricResultEntry] = sorted(
            r.entries,
            key=lambda e: _sort_key_for_value(value=e.value),
        )
        lines.append("| # | Task | Variant | Value |")
        lines.append("|---|------|---------|-------|")
        for rank, entry in enumerate(sorted_entries, start=1):
            variant_str: str = entry.variant_label if entry.variant_label is not None else ""
            value_str: str = str(entry.value) if entry.value is not None else "—"
            lines.append(
                f"| {rank} | `{entry.task_id}` | {variant_str} | {value_str} |",
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Aggregate metric values across all tasks",
    )
    add_output_format_arg(parser=parser)
    add_detail_level_arg(parser=parser)
    parser.add_argument(
        "--metric-keys",
        nargs="+",
        default=None,
        help="Filter by metric keys (exact match)",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Filter by task IDs (exact match)",
    )

    args: argparse.Namespace = parser.parse_args()
    output_format: str = args.format
    detail_level: str = args.detail
    filter_metric_keys: list[str] | None = args.metric_keys
    filter_task_ids: list[str] | None = args.task_ids

    if detail_level == DETAIL_LEVEL_SHORT:
        short_results: list[MetricResultsShort] = aggregate_metric_results_short(
            filter_metric_keys=filter_metric_keys,
            filter_task_ids=filter_task_ids,
        )
        if output_format == OUTPUT_FORMAT_JSON:
            print(_format_short_json(results=short_results))
        elif output_format == OUTPUT_FORMAT_MARKDOWN:
            short_full: list[MetricResultsFull] = aggregate_metric_results_full(
                filter_metric_keys=filter_metric_keys,
                filter_task_ids=filter_task_ids,
            )
            print(_format_full_markdown(results=short_full))
        else:
            print(f"Unknown format: {output_format}", file=sys.stderr)
            sys.exit(1)
    elif detail_level == DETAIL_LEVEL_FULL:
        full_results: list[MetricResultsFull] = aggregate_metric_results_full(
            filter_metric_keys=filter_metric_keys,
            filter_task_ids=filter_task_ids,
        )
        if output_format == OUTPUT_FORMAT_JSON:
            print(_format_full_json(results=full_results))
        elif output_format == OUTPUT_FORMAT_MARKDOWN:
            print(_format_full_markdown(results=full_results))
        else:
            print(f"Unknown format: {output_format}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown detail level: {detail_level}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
