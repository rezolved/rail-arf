"""Verificator entrypoint for the latency_benchmark_run asset type."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from arf.scripts.verificators.common.json_utils import load_json_file
from arf.scripts.verificators.common.paths import (
    latency_benchmark_run_asset_dir,
    latency_benchmark_run_base_dir,
    latency_benchmark_run_details_path,
    latency_benchmark_run_raw_requests_path,
)
from arf.scripts.verificators.common.reporting import (
    print_verification_result,
)
from arf.scripts.verificators.common.types import (
    Diagnostic,
    DiagnosticCode,
    Severity,
    VerificationResult,
)
from meta.asset_types.latency_benchmark_run.verify_details import (
    verify_latency_benchmark_run_details,
)
from meta.asset_types.latency_benchmark_run.verify_summary import (
    verify_latency_benchmark_run_summary,
)

# ---------------------------------------------------------------------------
# Folder-level diagnostic codes
# ---------------------------------------------------------------------------


_PREFIX: str = "LBR"

LBR_E003: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=3,
)
LBR_W001: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=1,
)
LBR_W007: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=7,
)


# ---------------------------------------------------------------------------
# Folder-level checks
# ---------------------------------------------------------------------------


def _resolve_raw_requests_path(*, run_id: str, task_id: str | None) -> Path:
    default_path: Path = latency_benchmark_run_raw_requests_path(
        run_id=run_id,
        task_id=task_id,
    )
    details_path: Path = latency_benchmark_run_details_path(
        run_id=run_id,
        task_id=task_id,
    )
    details: dict[str, Any] | None = load_json_file(file_path=details_path)
    if details is None:
        return default_path
    declared: object = details.get("raw_requests_path")
    if not isinstance(declared, str) or len(declared) == 0:
        return default_path
    return default_path.parent / declared


def _check_raw_requests_file(
    *,
    run_id: str,
    task_id: str | None,
) -> list[Diagnostic]:
    raw_path: Path = _resolve_raw_requests_path(run_id=run_id, task_id=task_id)
    if not raw_path.exists():
        return [
            Diagnostic(
                code=LBR_E003,
                message=f"raw_requests.jsonl does not exist: {raw_path}",
                file_path=raw_path,
            ),
        ]

    details_path: Path = latency_benchmark_run_details_path(
        run_id=run_id,
        task_id=task_id,
    )
    details: dict[str, Any] | None = load_json_file(file_path=details_path)
    expected_total: int | None = None
    if details is not None:
        total_value: object = details.get("total_requests")
        if isinstance(total_value, int) and not isinstance(total_value, bool):
            expected_total = total_value

    try:
        content: str = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    non_empty_lines: list[str] = [line for line in content.splitlines() if len(line.strip()) > 0]
    malformed_count: int = 0
    for line in non_empty_lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            malformed_count += 1

    diagnostics: list[Diagnostic] = []
    if expected_total is not None and len(non_empty_lines) != expected_total:
        diagnostics.append(
            Diagnostic(
                code=LBR_W001,
                message=(
                    f"raw_requests.jsonl has {len(non_empty_lines)} non-empty lines "
                    f"but details.json declares total_requests={expected_total}"
                ),
                file_path=raw_path,
            ),
        )
    if malformed_count > 0:
        diagnostics.append(
            Diagnostic(
                code=LBR_W007,
                message=(f"raw_requests.jsonl has {malformed_count} malformed JSON line(s)"),
                file_path=raw_path,
            ),
        )
    return diagnostics


# ---------------------------------------------------------------------------
# Main verification function
# ---------------------------------------------------------------------------


def verify_latency_benchmark_run_asset(
    *,
    run_id: str,
    task_id: str | None = None,
) -> VerificationResult:
    asset_dir: Path = latency_benchmark_run_asset_dir(
        run_id=run_id,
        task_id=task_id,
    )
    diagnostics: list[Diagnostic] = []

    diagnostics.extend(
        verify_latency_benchmark_run_details(
            run_id=run_id,
            task_id=task_id,
        ),
    )
    diagnostics.extend(
        verify_latency_benchmark_run_summary(
            run_id=run_id,
            task_id=task_id,
        ),
    )
    diagnostics.extend(
        _check_raw_requests_file(
            run_id=run_id,
            task_id=task_id,
        ),
    )

    return VerificationResult(
        file_path=asset_dir,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _discover_run_ids(*, task_id: str | None) -> list[str]:
    base_dir: Path = latency_benchmark_run_base_dir(task_id=task_id)
    if not base_dir.exists():
        return []
    return sorted(d.name for d in base_dir.iterdir() if d.is_dir() and not d.name.startswith("."))


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Verify latency-benchmark-run asset folder(s)",
    )
    parser.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help=(
            "Run ID (folder name) to verify. If omitted, verifies all runs in the target directory."
        ),
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help=(
            "Task ID to locate runs in "
            "tasks/<task_id>/assets/latency_benchmark_run/. "
            "If omitted, looks in top-level assets/latency_benchmark_run/."
        ),
    )
    args: argparse.Namespace = parser.parse_args()

    task_id: str | None = args.task_id
    run_ids: list[str]
    if args.run_id is not None:
        run_ids = [args.run_id]
    else:
        run_ids = _discover_run_ids(task_id=task_id)
        if len(run_ids) == 0:
            base_dir: Path = latency_benchmark_run_base_dir(task_id=task_id)
            print(f"No latency-benchmark-run assets found in {base_dir}")
            sys.exit(0)

    all_passed: bool = True
    for run_id in run_ids:
        result: VerificationResult = verify_latency_benchmark_run_asset(
            run_id=run_id,
            task_id=task_id,
        )
        print_verification_result(result=result)
        if not result.passed:
            all_passed = False

    if all_passed:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
