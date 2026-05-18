"""Verify the details.json of a latency-benchmark-run asset."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from arf.scripts.verificators.common.json_utils import (
    check_required_fields,
    load_json_file,
)
from arf.scripts.verificators.common.paths import (
    CATEGORIES_DIR,
    TASKS_DIR,
    latency_benchmark_run_details_path,
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

VLLM_REF_TASK_ID_FIELD: str = "task_id"
VLLM_REF_CONFIG_ID_FIELD: str = "config_id"

ENDPOINT_KIND_EXTERNAL: str = "external_provider"
ENDPOINT_KIND_LOCAL_VLLM: str = "local_vllm_config"
ALLOWED_ENDPOINT_KINDS: list[str] = [
    ENDPOINT_KIND_EXTERNAL,
    ENDPOINT_KIND_LOCAL_VLLM,
]

REQUIRED_FIELDS: list[str] = [
    SPEC_VERSION_FIELD,
    RUN_ID_FIELD,
    ENDPOINT_KIND_FIELD,
    ENDPOINT_LABEL_FIELD,
    VLLM_CONFIG_REF_FIELD,
    HARNESS_FIELD,
    HARNESS_VERSION_FIELD,
    CONCURRENCY_FIELD,
    DURATION_SECONDS_FIELD,
    WARMUP_SECONDS_FIELD,
    PROMPT_DATASET_REF_FIELD,
    START_TIME_FIELD,
    END_TIME_FIELD,
    TOTAL_REQUESTS_FIELD,
    SUCCESSFUL_REQUESTS_FIELD,
    SUMMARY_PATH_FIELD,
    RAW_REQUESTS_PATH_FIELD,
    CATEGORIES_FIELD,
    ADDED_BY_TASK_FIELD,
    DATE_ADDED_FIELD,
]

MIN_SUCCESS_RATE: float = 0.95

_ISO_DATETIME_PATTERN: re.Pattern[str] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$",
)

# ---------------------------------------------------------------------------
# Diagnostic codes
# ---------------------------------------------------------------------------

_PREFIX: str = "LBR"


def _err(number: int) -> DiagnosticCode:
    return DiagnosticCode(prefix=_PREFIX, severity=Severity.ERROR, number=number)


def _warn(number: int) -> DiagnosticCode:
    return DiagnosticCode(prefix=_PREFIX, severity=Severity.WARNING, number=number)


LBR_E001: DiagnosticCode = _err(1)
LBR_E004: DiagnosticCode = _err(4)
LBR_E005: DiagnosticCode = _err(5)
LBR_E006: DiagnosticCode = _err(6)
LBR_E007: DiagnosticCode = _err(7)
LBR_E008: DiagnosticCode = _err(8)
LBR_E009: DiagnosticCode = _err(9)
LBR_E011: DiagnosticCode = _err(11)
LBR_E012: DiagnosticCode = _err(12)
LBR_E013: DiagnosticCode = _err(13)
LBR_E014: DiagnosticCode = _err(14)

LBR_W003: DiagnosticCode = _warn(3)
LBR_W004: DiagnosticCode = _warn(4)
LBR_W005: DiagnosticCode = _warn(5)
LBR_W006: DiagnosticCode = _warn(6)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_required_fields(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    # vllm_config_ref is allowed to be null, so handle separately
    missing: list[str] = check_required_fields(
        data=data,
        required_fields=[f for f in REQUIRED_FIELDS if f != SPEC_VERSION_FIELD],
    )
    diagnostics: list[Diagnostic] = []
    for field_name in missing:
        diagnostics.append(
            Diagnostic(
                code=LBR_E005,
                message=f"Required field missing: '{field_name}'",
                file_path=file_path,
            ),
        )
    return diagnostics


def _check_spec_version(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    if SPEC_VERSION_FIELD not in data:
        return [
            Diagnostic(
                code=LBR_E006,
                message="spec_version is missing from details.json",
                file_path=file_path,
            ),
        ]
    return []


def _check_run_id_match(
    *,
    data: dict[str, Any],
    run_id: str,
    file_path: Path,
) -> list[Diagnostic]:
    json_id: object = data.get(RUN_ID_FIELD)
    if json_id is None:
        return []
    if str(json_id) != run_id:
        return [
            Diagnostic(
                code=LBR_E004,
                message=(
                    f"run_id '{json_id}' in details.json does not match folder name '{run_id}'"
                ),
                file_path=file_path,
            ),
        ]
    return []


def _check_endpoint_kind(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    kind: object = data.get(ENDPOINT_KIND_FIELD)
    if kind is None:
        return []
    if not isinstance(kind, str) or kind not in ALLOWED_ENDPOINT_KINDS:
        return [
            Diagnostic(
                code=LBR_E009,
                message=(
                    f"endpoint_kind '{kind}' is not one of: {', '.join(ALLOWED_ENDPOINT_KINDS)}"
                ),
                file_path=file_path,
            ),
        ]
    return []


def _vllm_ref_is_well_formed(*, ref: object) -> bool:
    if not isinstance(ref, dict):
        return False
    task_id: object = ref.get(VLLM_REF_TASK_ID_FIELD)
    config_id: object = ref.get(VLLM_REF_CONFIG_ID_FIELD)
    return (
        isinstance(task_id, str)
        and len(task_id) > 0
        and isinstance(config_id, str)
        and len(config_id) > 0
    )


def _check_vllm_config_ref(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    kind: object = data.get(ENDPOINT_KIND_FIELD)
    ref: object = data.get(VLLM_CONFIG_REF_FIELD)
    diagnostics: list[Diagnostic] = []
    if kind == ENDPOINT_KIND_LOCAL_VLLM:
        if ref is None or not _vllm_ref_is_well_formed(ref=ref):
            diagnostics.append(
                Diagnostic(
                    code=LBR_E007,
                    message=(
                        "endpoint_kind is 'local_vllm_config' but "
                        "vllm_config_ref is null or missing required fields "
                        "(task_id, config_id)"
                    ),
                    file_path=file_path,
                ),
            )
            return diagnostics
        # Structurally valid: check that the target exists
        assert isinstance(ref, dict)
        target_task_id: str = str(ref[VLLM_REF_TASK_ID_FIELD])
        target_config_id: str = str(ref[VLLM_REF_CONFIG_ID_FIELD])
        target_dir: Path = TASKS_DIR / target_task_id / "assets" / "vllm_config" / target_config_id
        if not target_dir.exists():
            diagnostics.append(
                Diagnostic(
                    code=LBR_E012,
                    message=(
                        f"vllm_config_ref target does not exist: {target_dir} "
                        f"(task_id='{target_task_id}', "
                        f"config_id='{target_config_id}')"
                    ),
                    file_path=file_path,
                ),
            )
    elif kind == ENDPOINT_KIND_EXTERNAL:
        if ref is not None:
            diagnostics.append(
                Diagnostic(
                    code=LBR_E008,
                    message=(
                        "endpoint_kind is 'external_provider' but vllm_config_ref is not null"
                    ),
                    file_path=file_path,
                ),
            )
    return diagnostics


def _check_concurrency(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    value: object = data.get(CONCURRENCY_FIELD)
    if value is None:
        return []
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return [
            Diagnostic(
                code=LBR_E013,
                message=f"concurrency must be a positive integer, got: {value!r}",
                file_path=file_path,
            ),
        ]
    return []


def _check_request_counts(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    total: object = data.get(TOTAL_REQUESTS_FIELD)
    successful: object = data.get(SUCCESSFUL_REQUESTS_FIELD)
    if not isinstance(total, int) or isinstance(total, bool):
        return []
    if not isinstance(successful, int) or isinstance(successful, bool):
        return []
    diagnostics: list[Diagnostic] = []
    if successful > total:
        diagnostics.append(
            Diagnostic(
                code=LBR_E011,
                message=(f"successful_requests ({successful}) exceeds total_requests ({total})"),
                file_path=file_path,
            ),
        )
        return diagnostics
    if total > 0 and (successful / total) < MIN_SUCCESS_RATE:
        diagnostics.append(
            Diagnostic(
                code=LBR_W005,
                message=(f"Success rate {successful}/{total} is below {MIN_SUCCESS_RATE:.0%}"),
                file_path=file_path,
            ),
        )
    return diagnostics


def _parse_iso_datetime(*, value: str) -> datetime | None:
    normalized: str = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _check_time_window(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    start: object = data.get(START_TIME_FIELD)
    end: object = data.get(END_TIME_FIELD)
    if not isinstance(start, str) or not isinstance(end, str):
        return []
    if _ISO_DATETIME_PATTERN.match(start) is None:
        return []
    if _ISO_DATETIME_PATTERN.match(end) is None:
        return []
    start_dt: datetime | None = _parse_iso_datetime(value=start)
    end_dt: datetime | None = _parse_iso_datetime(value=end)
    if start_dt is None or end_dt is None:
        return []
    if end_dt < start_dt:
        return [
            Diagnostic(
                code=LBR_E014,
                message=(f"end_time_utc ({end}) is earlier than start_time_utc ({start})"),
                file_path=file_path,
            ),
        ]
    return []


def _check_categories(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    categories: object = data.get(CATEGORIES_FIELD)
    if not isinstance(categories, list):
        return []
    diagnostics: list[Diagnostic] = []
    for category in categories:
        if not isinstance(category, str):
            continue
        category_dir: Path = CATEGORIES_DIR / category
        if not category_dir.exists():
            diagnostics.append(
                Diagnostic(
                    code=LBR_W004,
                    message=f"Category '{category}' does not exist in meta/categories/",
                    file_path=file_path,
                ),
            )
    return diagnostics


def _check_warmup(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    if WARMUP_SECONDS_FIELD not in data:
        return []
    if data[WARMUP_SECONDS_FIELD] is None:
        return [
            Diagnostic(
                code=LBR_W003,
                message="warmup_seconds is null; warm-up handling should be explicit",
                file_path=file_path,
            ),
        ]
    return []


def _check_prompt_dataset_ref(
    *,
    data: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    if PROMPT_DATASET_REF_FIELD not in data:
        return []
    if data[PROMPT_DATASET_REF_FIELD] is None:
        return [
            Diagnostic(
                code=LBR_W006,
                message=(
                    "prompt_dataset_ref is null; run is not reproducible from a "
                    "recorded prompt corpus"
                ),
                file_path=file_path,
            ),
        ]
    return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def verify_latency_benchmark_run_details(
    *,
    run_id: str,
    task_id: str | None = None,
) -> list[Diagnostic]:
    file_path: Path = latency_benchmark_run_details_path(
        run_id=run_id,
        task_id=task_id,
    )

    if not file_path.exists():
        return [
            Diagnostic(
                code=LBR_E001,
                message=f"details.json does not exist: {file_path}",
                file_path=file_path,
            ),
        ]

    data: dict[str, Any] | None = load_json_file(file_path=file_path)
    if data is None:
        return [
            Diagnostic(
                code=LBR_E001,
                message=f"details.json is not valid JSON: {file_path}",
                file_path=file_path,
            ),
        ]

    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_check_required_fields(data=data, file_path=file_path))
    diagnostics.extend(_check_spec_version(data=data, file_path=file_path))
    diagnostics.extend(_check_run_id_match(data=data, run_id=run_id, file_path=file_path))
    diagnostics.extend(_check_endpoint_kind(data=data, file_path=file_path))
    diagnostics.extend(_check_vllm_config_ref(data=data, file_path=file_path))
    diagnostics.extend(_check_concurrency(data=data, file_path=file_path))
    diagnostics.extend(_check_request_counts(data=data, file_path=file_path))
    diagnostics.extend(_check_time_window(data=data, file_path=file_path))
    diagnostics.extend(_check_categories(data=data, file_path=file_path))
    diagnostics.extend(_check_warmup(data=data, file_path=file_path))
    diagnostics.extend(_check_prompt_dataset_ref(data=data, file_path=file_path))
    return diagnostics
