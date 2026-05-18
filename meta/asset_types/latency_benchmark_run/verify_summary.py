"""Verify the summary.json of a latency-benchmark-run asset."""

from pathlib import Path
from typing import Any

from arf.scripts.verificators.common.json_utils import load_json_file
from arf.scripts.verificators.common.paths import (
    latency_benchmark_run_details_path,
    latency_benchmark_run_summary_path,
)
from arf.scripts.verificators.common.types import (
    Diagnostic,
    DiagnosticCode,
    Severity,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPEC_VERSION_FIELD: str = "spec_version"
LATENCY_AVG_FIELD: str = "latency_avg_seconds"
LATENCY_P50_FIELD: str = "latency_p50_seconds"
LATENCY_P95_FIELD: str = "latency_p95_seconds"
LATENCY_P99_FIELD: str = "latency_p99_seconds"
TTFT_MEDIAN_FIELD: str = "ttft_median_seconds"
TTFT_P95_FIELD: str = "ttft_p95_seconds"
TTFT_P99_FIELD: str = "ttft_p99_seconds"
TOKENS_PER_SECOND_FIELD: str = "tokens_per_second"
INPUT_TOKENS_TOTAL_FIELD: str = "input_tokens_total"
OUTPUT_TOKENS_TOTAL_FIELD: str = "output_tokens_total"

OUTPUT_TOKENS_TOTAL_DETAILS_KEY: str = OUTPUT_TOKENS_TOTAL_FIELD

REQUIRED_FLOAT_FIELDS: list[str] = [
    LATENCY_AVG_FIELD,
    LATENCY_P50_FIELD,
    LATENCY_P95_FIELD,
    LATENCY_P99_FIELD,
    TOKENS_PER_SECOND_FIELD,
]

REQUIRED_NULLABLE_FLOAT_FIELDS: list[str] = [
    TTFT_MEDIAN_FIELD,
    TTFT_P95_FIELD,
    TTFT_P99_FIELD,
]

REQUIRED_INT_FIELDS: list[str] = [
    INPUT_TOKENS_TOTAL_FIELD,
    OUTPUT_TOKENS_TOTAL_FIELD,
]


# ---------------------------------------------------------------------------
# Diagnostic codes
# ---------------------------------------------------------------------------

_PREFIX: str = "LBR"


def _err(number: int) -> DiagnosticCode:
    return DiagnosticCode(prefix=_PREFIX, severity=Severity.ERROR, number=number)


def _warn(number: int) -> DiagnosticCode:
    return DiagnosticCode(prefix=_PREFIX, severity=Severity.WARNING, number=number)


LBR_E002: DiagnosticCode = _err(2)
LBR_E006: DiagnosticCode = _err(6)
LBR_E010: DiagnosticCode = _err(10)
LBR_E015: DiagnosticCode = _err(15)
LBR_E016: DiagnosticCode = _err(16)

LBR_W002: DiagnosticCode = _warn(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_float(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_spec_version(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    if SPEC_VERSION_FIELD not in data:
        return [
            Diagnostic(
                code=LBR_E006,
                message="spec_version is missing from summary.json",
                file_path=file_path,
            ),
        ]
    return []


def _check_required_fields_and_types(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for field_name in REQUIRED_FLOAT_FIELDS:
        if field_name not in data:
            diagnostics.append(
                Diagnostic(
                    code=LBR_E015,
                    message=f"Required summary field missing: '{field_name}'",
                    file_path=file_path,
                ),
            )
            continue
        if not _is_float(data[field_name]):
            diagnostics.append(
                Diagnostic(
                    code=LBR_E015,
                    message=(
                        f"Summary field '{field_name}' has wrong type "
                        f"(expected number, got {type(data[field_name]).__name__})"
                    ),
                    file_path=file_path,
                ),
            )
    for field_name in REQUIRED_NULLABLE_FLOAT_FIELDS:
        if field_name not in data:
            diagnostics.append(
                Diagnostic(
                    code=LBR_E015,
                    message=f"Required summary field missing: '{field_name}'",
                    file_path=file_path,
                ),
            )
            continue
        value: object = data[field_name]
        if value is not None and not _is_float(value):
            diagnostics.append(
                Diagnostic(
                    code=LBR_E015,
                    message=(
                        f"Summary field '{field_name}' has wrong type "
                        f"(expected number or null, got {type(value).__name__})"
                    ),
                    file_path=file_path,
                ),
            )
    for field_name in REQUIRED_INT_FIELDS:
        if field_name not in data:
            diagnostics.append(
                Diagnostic(
                    code=LBR_E015,
                    message=f"Required summary field missing: '{field_name}'",
                    file_path=file_path,
                ),
            )
            continue
        value = data[field_name]
        if not _is_int(value) or (isinstance(value, int) and value < 0):
            diagnostics.append(
                Diagnostic(
                    code=LBR_E015,
                    message=(
                        f"Summary field '{field_name}' must be a non-negative integer, "
                        f"got {value!r}"
                    ),
                    file_path=file_path,
                ),
            )
    return diagnostics


def _check_latency_monotonic(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    p50: object = data.get(LATENCY_P50_FIELD)
    p95: object = data.get(LATENCY_P95_FIELD)
    p99: object = data.get(LATENCY_P99_FIELD)
    if not (_is_float(p50) and _is_float(p95) and _is_float(p99)):
        return []
    p50_f: float = float(p50)  # type: ignore[arg-type]
    p95_f: float = float(p95)  # type: ignore[arg-type]
    p99_f: float = float(p99)  # type: ignore[arg-type]
    if not (p50_f <= p95_f <= p99_f):
        return [
            Diagnostic(
                code=LBR_E010,
                message=(
                    f"Latency percentiles are not monotonic: p50={p50_f}, p95={p95_f}, p99={p99_f}"
                ),
                file_path=file_path,
            ),
        ]
    return []


def _check_ttft_monotonic(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    median: object = data.get(TTFT_MEDIAN_FIELD)
    p95: object = data.get(TTFT_P95_FIELD)
    p99: object = data.get(TTFT_P99_FIELD)
    is_null: list[bool] = [median is None, p95 is None, p99 is None]
    # All null is OK
    if all(is_null):
        return []
    # Mixed null/non-null is an inconsistency
    if any(is_null) and not all(is_null):
        return [
            Diagnostic(
                code=LBR_E016,
                message=(
                    f"TTFT percentiles are inconsistent (some null, some not): "
                    f"ttft_median_seconds={median!r}, "
                    f"ttft_p95_seconds={p95!r}, "
                    f"ttft_p99_seconds={p99!r}"
                ),
                file_path=file_path,
            ),
        ]
    if not (_is_float(median) and _is_float(p95) and _is_float(p99)):
        return []
    m_f: float = float(median)  # type: ignore[arg-type]
    p95_f: float = float(p95)  # type: ignore[arg-type]
    p99_f: float = float(p99)  # type: ignore[arg-type]
    if not (m_f <= p95_f <= p99_f):
        return [
            Diagnostic(
                code=LBR_E016,
                message=(
                    f"TTFT percentiles are not monotonic: median={m_f}, p95={p95_f}, p99={p99_f}"
                ),
                file_path=file_path,
            ),
        ]
    return []


def _check_ttft_with_outputs(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    median: object = data.get(TTFT_MEDIAN_FIELD)
    output_total: object = data.get(OUTPUT_TOKENS_TOTAL_FIELD)
    if median is None and isinstance(output_total, int) and output_total > 0:
        return [
            Diagnostic(
                code=LBR_W002,
                message=(
                    "ttft_median_seconds is null while output_tokens_total > 0; "
                    "streaming TTFT should be measurable"
                ),
                file_path=file_path,
            ),
        ]
    return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _resolve_summary_path(*, run_id: str, task_id: str | None) -> Path:
    details_path: Path = latency_benchmark_run_details_path(
        run_id=run_id,
        task_id=task_id,
    )
    default_summary: Path = latency_benchmark_run_summary_path(
        run_id=run_id,
        task_id=task_id,
    )
    details_data: dict[str, Any] | None = load_json_file(file_path=details_path)
    if details_data is None:
        return default_summary
    declared: object = details_data.get("summary_path")
    if not isinstance(declared, str) or len(declared) == 0:
        return default_summary
    return default_summary.parent / declared


def verify_latency_benchmark_run_summary(
    *,
    run_id: str,
    task_id: str | None = None,
) -> list[Diagnostic]:
    file_path: Path = _resolve_summary_path(run_id=run_id, task_id=task_id)

    if not file_path.exists():
        return [
            Diagnostic(
                code=LBR_E002,
                message=f"summary.json does not exist: {file_path}",
                file_path=file_path,
            ),
        ]

    data: dict[str, Any] | None = load_json_file(file_path=file_path)
    if data is None:
        return [
            Diagnostic(
                code=LBR_E002,
                message=f"summary.json is not valid JSON: {file_path}",
                file_path=file_path,
            ),
        ]

    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_check_spec_version(data=data, file_path=file_path))
    diagnostics.extend(_check_required_fields_and_types(data=data, file_path=file_path))
    diagnostics.extend(_check_latency_monotonic(data=data, file_path=file_path))
    diagnostics.extend(_check_ttft_monotonic(data=data, file_path=file_path))
    diagnostics.extend(_check_ttft_with_outputs(data=data, file_path=file_path))
    return diagnostics
