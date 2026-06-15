"""Verify machine_log.json cost attribution for any task that declared a GPU provider.

Encodes LESSONS.md (cost attribution: machine provisioning/teardown logs must
exist with timing + cost) as a check that runs on the task branch.

Emits error `MCH-E101` if `logs/steps/*setup-machines*/machine_log.json` is
missing on a task whose `plan/plan.md` references a compute provider
(`azure_ml`, `vast_ai`, `nebius`) — see Section 5 (Remote Machines).

`machine_log.json` is a JSON array of per-machine entries (one entry per
provisioned machine). Each entry must carry the cost-attribution fields:
* `provider`
* `instance_id`
* `created_at`
* `destroyed_at`
* `total_duration_hours`
* `total_cost_usd`

Usage:
    uv run python -u -m arf.scripts.verificators.verify_machine_log_cost_attribution <task_id>
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

TASKS_DIR_NAME: str = "tasks"
LOGS_DIR_NAME: str = "logs"
STEPS_DIR_NAME: str = "steps"
PLAN_DIR_NAME: str = "plan"
PLAN_FILE_NAME: str = "plan.md"
MACHINE_LOG_FILE_NAME: str = "machine_log.json"
SETUP_MACHINES_SUBSTRING: str = "setup-machines"

REQUIRED_FIELDS: list[str] = [
    "provider",
    "instance_id",
    "created_at",
    "destroyed_at",
    "total_duration_hours",
    "total_cost_usd",
]

COMPUTE_PROVIDER_KEYWORDS: list[str] = [
    "azure_ml",
    "azure-ml",
    "vast_ai",
    "vast-ai",
    "vastai",
    "nebius",
]

ERROR_CODE_MISSING_LOG: str = "MCH-E101"
ERROR_CODE_MISSING_FIELD: str = "MCH-E102"
ERROR_CODE_UNREADABLE: str = "MCH-E103"
WARNING_CODE_NULL_COST: str = "MCH-W101"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: str
    code: str
    file_path: Path
    message: str


def _plan_references_compute_provider(*, plan_path: Path) -> bool:
    if not plan_path.is_file():
        return False
    text: str = plan_path.read_text(encoding="utf-8").lower()
    return any(keyword in text for keyword in COMPUTE_PROVIDER_KEYWORDS)


def _find_machine_log_files(*, steps_dir: Path) -> list[Path]:
    if not steps_dir.is_dir():
        return []
    matches: list[Path] = []
    for step_dir in sorted(steps_dir.iterdir()):
        if not step_dir.is_dir():
            continue
        if SETUP_MACHINES_SUBSTRING not in step_dir.name:
            continue
        log_path: Path = step_dir / MACHINE_LOG_FILE_NAME
        if log_path.is_file():
            matches.append(log_path)
    return matches


def _check_machine_log_entry(
    *,
    log_path: Path,
    entry: object,
    index: int,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not isinstance(entry, dict):
        diagnostics.append(
            Diagnostic(
                severity="error",
                code=ERROR_CODE_UNREADABLE,
                file_path=log_path,
                message=f"machine_log.json entry {index} is not a JSON object",
            )
        )
        return diagnostics
    for field in REQUIRED_FIELDS:
        if field not in entry:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    code=ERROR_CODE_MISSING_FIELD,
                    file_path=log_path,
                    message=f"entry {index}: required field {field!r} is missing",
                )
            )
        elif entry[field] is None:
            severity: str = "warning" if field == "total_cost_usd" else "error"
            code: str = (
                WARNING_CODE_NULL_COST if field == "total_cost_usd" else ERROR_CODE_MISSING_FIELD
            )
            diagnostics.append(
                Diagnostic(
                    severity=severity,
                    code=code,
                    file_path=log_path,
                    message=f"entry {index}: field {field!r} is null",
                )
            )
    return diagnostics


def _check_machine_log_fields(*, log_path: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    try:
        raw: str = log_path.read_text(encoding="utf-8")
        data: object = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(
            Diagnostic(
                severity="error",
                code=ERROR_CODE_UNREADABLE,
                file_path=log_path,
                message=f"cannot read machine_log.json: {exc}",
            )
        )
        return diagnostics
    if not isinstance(data, list):
        diagnostics.append(
            Diagnostic(
                severity="error",
                code=ERROR_CODE_UNREADABLE,
                file_path=log_path,
                message="machine_log.json top-level value is not a JSON array",
            )
        )
        return diagnostics
    # A present-but-empty array records a setup-machines step that acquired no
    # machine (e.g. a blocked attempt before a fallback). There is no cost to
    # attribute, so it is acceptable; only a *missing* log on a compute task is
    # an error (MCH-E101, handled in verify_task).
    for index, entry in enumerate(data):
        diagnostics.extend(_check_machine_log_entry(log_path=log_path, entry=entry, index=index))
    return diagnostics


def verify_task(*, task_id: str, repo_root: Path) -> list[Diagnostic]:
    task_dir: Path = repo_root / TASKS_DIR_NAME / task_id
    assert task_dir.is_dir(), f"task folder exists: {task_dir}"
    plan_path: Path = task_dir / PLAN_DIR_NAME / PLAN_FILE_NAME
    declares_compute: bool = _plan_references_compute_provider(plan_path=plan_path)
    if not declares_compute:
        return []
    steps_dir: Path = task_dir / LOGS_DIR_NAME / STEPS_DIR_NAME
    log_files: list[Path] = _find_machine_log_files(steps_dir=steps_dir)
    diagnostics: list[Diagnostic] = []
    if len(log_files) == 0:
        diagnostics.append(
            Diagnostic(
                severity="error",
                code=ERROR_CODE_MISSING_LOG,
                file_path=steps_dir,
                message=(
                    "task plan references a compute provider but no machine_log.json "
                    "exists under logs/steps/*setup-machines*/. Provisioning and cost "
                    "attribution are mandatory for any task that uses GPU compute."
                ),
            )
        )
        return diagnostics
    for log_path in log_files:
        diagnostics.extend(_check_machine_log_fields(log_path=log_path))
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
            "verify_machine_log_cost_attribution: OK — no compute provider declared, "
            "or machine_log.json present with all required fields."
        )
    return 1 if error_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
