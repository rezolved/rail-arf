"""Verify every `latency_benchmark_run` asset in a task records warmup metadata.

Encodes LESSONS.md Lesson 1 (cold vs. warm cache invalidates pairing) as a check
that runs on the task branch. Emits warning `LBR-W101` for any benchmark asset
whose `details.json` is missing `warmup_requests`, has `warmup_requests < 1`,
or has a null `warmup_corpus_ref`.

Usage:
    uv run python -u -m arf.scripts.verificators.verify_latency_benchmark_run_warmup <task_id>

Exit codes:
    0 on success (no errors; warnings allowed).
    Non-zero if a structural error is detected (e.g., unreadable JSON).
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

WARMUP_REQUESTS_FIELD: str = "warmup_requests"
WARMUP_CORPUS_REF_FIELD: str = "warmup_corpus_ref"
DETAILS_FIELD: str = "details"

ASSET_KIND: str = "latency_benchmark_run"
DETAILS_FILENAME: str = "details.json"
TASKS_DIR_NAME: str = "tasks"
ASSETS_DIR_NAME: str = "assets"

WARNING_CODE_MISSING: str = "LBR-W101"
WARNING_CODE_ZERO: str = "LBR-W102"
WARNING_CODE_NO_CORPUS_REF: str = "LBR-W103"
ERROR_CODE_UNREADABLE: str = "LBR-E101"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: str
    code: str
    file_path: Path
    message: str


def _read_details(*, details_path: Path) -> tuple[dict[str, object] | None, Diagnostic | None]:
    try:
        raw: str = details_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, Diagnostic(
            severity="error",
            code=ERROR_CODE_UNREADABLE,
            file_path=details_path,
            message=f"cannot read details.json: {exc}",
        )
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, Diagnostic(
            severity="error",
            code=ERROR_CODE_UNREADABLE,
            file_path=details_path,
            message=f"details.json is not valid JSON: {exc}",
        )
    if not isinstance(data, dict):
        return None, Diagnostic(
            severity="error",
            code=ERROR_CODE_UNREADABLE,
            file_path=details_path,
            message="details.json top-level value is not a JSON object",
        )
    return data, None


def _check_one_asset(*, details_path: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    data, read_error = _read_details(details_path=details_path)
    if read_error is not None:
        diagnostics.append(read_error)
        return diagnostics
    assert data is not None, "data is non-None when read_error is None"

    details: object = data.get(DETAILS_FIELD)
    container: dict[str, object] = details if isinstance(details, dict) else data

    warmup_value: object = container.get(WARMUP_REQUESTS_FIELD)
    corpus_ref_value: object = container.get(WARMUP_CORPUS_REF_FIELD)

    if warmup_value is None:
        diagnostics.append(
            Diagnostic(
                severity="warning",
                code=WARNING_CODE_MISSING,
                file_path=details_path,
                message=(
                    f"{WARMUP_REQUESTS_FIELD} is missing or null; "
                    "warmup-N protocol is required (see LESSONS.md Lesson 1)"
                ),
            )
        )
    elif isinstance(warmup_value, int) and warmup_value < 1:
        diagnostics.append(
            Diagnostic(
                severity="warning",
                code=WARNING_CODE_ZERO,
                file_path=details_path,
                message=(
                    f"{WARMUP_REQUESTS_FIELD}={warmup_value} (< 1); "
                    "the warmup phase must run at least one request"
                ),
            )
        )

    if corpus_ref_value is None or (
        isinstance(corpus_ref_value, str) and len(corpus_ref_value) == 0
    ):
        diagnostics.append(
            Diagnostic(
                severity="warning",
                code=WARNING_CODE_NO_CORPUS_REF,
                file_path=details_path,
                message=(
                    f"{WARMUP_CORPUS_REF_FIELD} is missing or empty; "
                    "the warmup corpus must be referenced for reproducibility"
                ),
            )
        )

    return diagnostics


def _find_asset_details_files(*, task_assets_dir: Path) -> list[Path]:
    target_dir: Path = task_assets_dir / ASSET_KIND
    if not target_dir.is_dir():
        return []
    matches: list[Path] = []
    for asset_dir in sorted(target_dir.iterdir()):
        if not asset_dir.is_dir():
            continue
        details_path: Path = asset_dir / DETAILS_FILENAME
        if details_path.is_file():
            matches.append(details_path)
    return matches


def verify_task(*, task_id: str, repo_root: Path) -> list[Diagnostic]:
    task_dir: Path = repo_root / TASKS_DIR_NAME / task_id
    assert task_dir.is_dir(), f"task folder exists: {task_dir}"
    assets_dir: Path = task_dir / ASSETS_DIR_NAME
    if not assets_dir.is_dir():
        return []
    details_files: list[Path] = _find_asset_details_files(task_assets_dir=assets_dir)
    diagnostics: list[Diagnostic] = []
    for details_path in details_files:
        diagnostics.extend(_check_one_asset(details_path=details_path))
    return diagnostics


def _print_diagnostics(*, diagnostics: list[Diagnostic]) -> int:
    error_count: int = 0
    for diag in diagnostics:
        print(f"[{diag.severity}] {diag.code} {diag.file_path}: {diag.message}")
        if diag.severity == "error":
            error_count += 1
    return error_count


def main(*, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", help="Task folder name under tasks/")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root (default: current working directory)",
    )
    args = parser.parse_args(argv)
    repo_root: Path = Path(args.repo_root).resolve()
    diagnostics: list[Diagnostic] = verify_task(task_id=args.task_id, repo_root=repo_root)
    error_count: int = _print_diagnostics(diagnostics=diagnostics)
    if error_count == 0 and len(diagnostics) == 0:
        print(
            f"verify_latency_benchmark_run_warmup: OK — no {ASSET_KIND} assets found "
            "or all assets carry warmup metadata."
        )
    return 1 if error_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
