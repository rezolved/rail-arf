"""Verificator for remote machine destruction.

Checks that all remote instances created during a task have been destroyed.
Supports Vast.ai (``vast_ai`` / legacy ``vast.ai``), Azure ML (``azure_ml``),
and Nebius Cloud (``nebius``) providers. Prevents cost leaks from forgotten
instances.

Usage:
    uv run python -m arf.scripts.verificators.verify_machines_destroyed <task_id>
    uv run python -m arf.scripts.verificators.verify_machines_destroyed --all

Exit codes:
    0 — no errors (warnings may be present)
    1 — one or more errors found
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from arf.scripts.verificators.common.paths import (
    TASKS_DIR,
    plan_path,
    remote_machines_path,
    step_logs_dir,
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PREFIX: str = "RM"

_FIELD_INSTANCE_ID: str = "instance_id"
_FIELD_DESTROYED_AT: str = "destroyed_at"
_FIELD_MACHINE_ID: str = "machine_id"
_FIELD_ACTUAL_STATUS: str = "actual_status"
_FIELD_PROVIDER: str = "provider"
_FIELD_SPEC_VERSION: str = "spec_version"
_FIELD_VM_NAME: str = "vm_name"

_UNKNOWN_ID: str = "unknown"
_ACTIVE_STATUSES: tuple[str, ...] = ("running", "loading")
_AZURE_ACTIVE_STATES: tuple[str, ...] = ("Running", "Starting")
_AZURE_INACTIVE_STATES: tuple[str, ...] = ("Stopped", "Deallocated")
_SETUP_MACHINES_STEP_NAME: str = "setup-machines"
_MACHINE_LOG_FILENAME: str = "machine_log.json"

_PROVIDER_VAST_AI: str = "vast_ai"
_PROVIDER_VAST_AI_LEGACY: str = "vast.ai"
_PROVIDER_AZURE_ML: str = "azure_ml"
_PROVIDER_NEBIUS: str = "nebius"
_VAST_PROVIDERS: tuple[str, ...] = (
    _PROVIDER_VAST_AI,
    _PROVIDER_VAST_AI_LEGACY,
)
_KNOWN_PROVIDERS: tuple[str, ...] = (
    _PROVIDER_VAST_AI,
    _PROVIDER_VAST_AI_LEGACY,
    _PROVIDER_AZURE_ML,
    _PROVIDER_NEBIUS,
)

_NEBIUS_ACTIVE_STATES: tuple[str, ...] = ("RUNNING", "STARTING", "PROVISIONING")
_FIELD_INSTANCE_ID_NEBIUS: str = "instance_id"

_SPEC_VERSION_CURRENT: str = "5"

VASTAI_SHOW_TIMEOUT: float = 15.0
AZ_ML_SHOW_TIMEOUT: float = 15.0
NEBIUS_SHOW_TIMEOUT: float = 15.0

_CODE_NO_DESTROYED_AT: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=1,
)
_CODE_INSTANCE_STILL_ACTIVE: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=2,
)
_CODE_MACHINE_LOG_INVALID: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=3,
)
_CODE_API_UNREACHABLE: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=1,
)
_CODE_MISSING_FIELD: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=4,
)
_CODE_INSTANCE_ID_MISMATCH: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=5,
)
_CODE_COST_MISMATCH: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=6,
)
_CODE_COST_EXCEEDS_ESTIMATE: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=2,
)
_CODE_LONG_RUNNING: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=3,
)
_CODE_RATIONALE_TOO_SHORT: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=4,
)
_CODE_FAILED_ATTEMPT_INCOMPLETE: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=5,
)
_CODE_NO_CHECKPOINT_PATH: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=6,
)
_CODE_UNKNOWN_PROVIDER: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=7,
)
_CODE_SPEC_VERSION_MISSING: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=7,
)

_REQUIRED_MACHINE_LOG_FIELDS: tuple[str, ...] = (
    "provider",
    "instance_id",
    "offer_id",
    "search_criteria",
    "selected_offer",
    "selection_rationale",
    "image",
    "disk_gb",
    "ssh_host",
    "ssh_port",
    "gpu_verified",
    "cuda_version",
    "created_at",
    "ready_at",
    "destroyed_at",
    "total_duration_hours",
    "total_cost_usd",
)
_COST_TOLERANCE: float = 0.01
_COST_OVERRUN_FACTOR: float = 1.5
_MAX_DURATION_HOURS: float = 12.0
_MIN_RATIONALE_LENGTH: int = 20
_CHECKPOINT_DURATION_THRESHOLD: float = 2.0
_FAILED_ATTEMPT_REQUIRED_FIELDS: tuple[str, ...] = (
    "offer_id",
    "failure_reason",
    "failure_phase",
    "timestamp",
)
_DOLLAR_PATTERN: re.Pattern[str] = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_machine_log(*, task_id: str) -> Path | None:
    steps_dir: Path = step_logs_dir(task_id=task_id)
    if not steps_dir.exists():
        return None
    for step_dir in sorted(steps_dir.iterdir()):
        if not step_dir.is_dir():
            continue
        if _SETUP_MACHINES_STEP_NAME in step_dir.name:
            log_path: Path = step_dir / _MACHINE_LOG_FILENAME
            if log_path.exists():
                return log_path
    return None


def _vastai_show(*, instance_id: str) -> str | None:
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["vastai", "show", "instance", instance_id, "--raw"],
            capture_output=True,
            text=True,
            timeout=VASTAI_SHOW_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        data: dict[str, Any] = json.loads(result.stdout)
        status: object = data.get(_FIELD_ACTUAL_STATUS)
        if isinstance(status, str):
            return status
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None


def _az_ml_show(*, vm_name: str) -> str | None:
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["az", "ml", "compute", "show", "--name", vm_name, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=AZ_ML_SHOW_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        data: dict[str, Any] = json.loads(result.stdout)
        state: object = data.get("state")
        if isinstance(state, str):
            return state
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None


def _nebius_show(*, instance_id: str, profile_name: str = "compute") -> str | None:
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            [
                "nebius",
                "-p",
                profile_name,
                "compute",
                "instance",
                "get",
                "--id",
                instance_id,
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=NEBIUS_SHOW_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        data: dict[str, Any] = json.loads(result.stdout)
        status: object = data.get("status")
        if isinstance(status, dict):
            state: object = status.get("state")
            if isinstance(state, str):
                return state
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None


def _check_instance_via_api(*, instance_id: str) -> str | None:
    """Vast.ai status check — monkey-patched by tests.

    Tests stub this attribute directly on the module to bypass the
    subprocess call. Production code calls it through ``_check_machine``.
    """
    return _vastai_show(instance_id=instance_id)


def _check_azure_vm_state(*, vm_name: str) -> str | None:
    """Azure ML compute state check — monkey-patched by tests."""
    return _az_ml_show(vm_name=vm_name)


def _check_nebius_instance_state(*, instance_id: str) -> str | None:
    """Nebius instance state check — monkey-patched by tests."""
    return _nebius_show(instance_id=instance_id)


def _normalise_provider(*, value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if value in _KNOWN_PROVIDERS:
        return value
    return None


# ---------------------------------------------------------------------------
# Per-machine destruction check
# ---------------------------------------------------------------------------


def _check_machine(
    *,
    entry: dict[str, Any],
    log_file: Path,
    diagnostics: list[Diagnostic],
) -> None:
    instance_id_str: str = str(
        entry.get(_FIELD_INSTANCE_ID, _UNKNOWN_ID),
    )
    destroyed_at: object = entry.get(_FIELD_DESTROYED_AT)
    provider_raw: object = entry.get(_FIELD_PROVIDER)
    provider: str | None = _normalise_provider(value=provider_raw)

    # RM-W007: spec_version missing or not current (informational warning).
    spec_version: object = entry.get(_FIELD_SPEC_VERSION)
    if spec_version != _SPEC_VERSION_CURRENT:
        diagnostics.append(
            Diagnostic(
                code=_CODE_SPEC_VERSION_MISSING,
                message=(
                    f"Machine {instance_id_str} spec_version is missing"
                    f" or not '{_SPEC_VERSION_CURRENT}' — legacy entry"
                ),
                file_path=log_file,
            ),
        )

    # RM-E007: provider is set but has an unrecognised value.
    # Missing provider is treated as legacy Vast.ai for backwards compatibility.
    if provider_raw is not None and provider is None:
        diagnostics.append(
            Diagnostic(
                code=_CODE_UNKNOWN_PROVIDER,
                message=(
                    f"Machine {instance_id_str} has unknown provider"
                    f" value '{provider_raw}' (expected one of"
                    f" {_KNOWN_PROVIDERS})"
                ),
                file_path=log_file,
            ),
        )
        return

    if provider is None or provider in _VAST_PROVIDERS:
        _check_machine_vast(
            instance_id=instance_id_str,
            destroyed_at=destroyed_at,
            log_file=log_file,
            diagnostics=diagnostics,
        )
    elif provider == _PROVIDER_AZURE_ML:
        vm_name_raw: object = entry.get(_FIELD_VM_NAME)
        vm_name: str = str(vm_name_raw) if vm_name_raw is not None else instance_id_str
        _check_machine_azure(
            instance_id=instance_id_str,
            vm_name=vm_name,
            destroyed_at=destroyed_at,
            log_file=log_file,
            diagnostics=diagnostics,
        )
    elif provider == _PROVIDER_NEBIUS:
        _check_machine_nebius(
            instance_id=instance_id_str,
            destroyed_at=destroyed_at,
            log_file=log_file,
            diagnostics=diagnostics,
        )


def _check_machine_vast(
    *,
    instance_id: str,
    destroyed_at: object,
    log_file: Path,
    diagnostics: list[Diagnostic],
) -> None:
    if destroyed_at is None:
        diagnostics.append(
            Diagnostic(
                code=_CODE_NO_DESTROYED_AT,
                message=(f"Machine {instance_id} has no destroyed_at timestamp"),
                file_path=log_file,
                detail=("Instance may still be running and incurring charges"),
            ),
        )
        api_status: str | None = _check_instance_via_api(
            instance_id=instance_id,
        )
        if api_status is not None and api_status in _ACTIVE_STATUSES:
            diagnostics.append(
                Diagnostic(
                    code=_CODE_INSTANCE_STILL_ACTIVE,
                    message=(f"Vast.ai confirms instance {instance_id} is still {api_status}"),
                    file_path=log_file,
                    detail=("Destroy this instance immediately to stop charges"),
                ),
            )
        return

    api_status = _check_instance_via_api(instance_id=instance_id)
    if api_status is None:
        diagnostics.append(
            Diagnostic(
                code=_CODE_API_UNREACHABLE,
                message=(f"Cannot verify destruction of {instance_id} — API unreachable"),
                file_path=log_file,
            ),
        )
    elif api_status in _ACTIVE_STATUSES:
        diagnostics.append(
            Diagnostic(
                code=_CODE_INSTANCE_STILL_ACTIVE,
                message=(
                    f"Instance {instance_id} has destroyed_at but"
                    f" Vast.ai reports status '{api_status}'"
                ),
                file_path=log_file,
                detail=("destroyed_at may be incorrect — verify and destroy if needed"),
            ),
        )


def _check_machine_azure(
    *,
    instance_id: str,
    vm_name: str,
    destroyed_at: object,
    log_file: Path,
    diagnostics: list[Diagnostic],
) -> None:
    if destroyed_at is None:
        diagnostics.append(
            Diagnostic(
                code=_CODE_NO_DESTROYED_AT,
                message=(f"Machine {instance_id} has no destroyed_at timestamp"),
                file_path=log_file,
                detail=("Azure ML VM may still be running and incurring charges"),
            ),
        )
        state: str | None = _check_azure_vm_state(vm_name=vm_name)
        if state is not None and state in _AZURE_ACTIVE_STATES:
            diagnostics.append(
                Diagnostic(
                    code=_CODE_INSTANCE_STILL_ACTIVE,
                    message=(f"Azure ML confirms VM {vm_name} is still in state '{state}'"),
                    file_path=log_file,
                    detail=("Stop or deallocate this VM immediately to stop charges"),
                ),
            )
        return

    state = _check_azure_vm_state(vm_name=vm_name)
    if state is None:
        # API unreachable — treat destroyed_at as authoritative (warning only).
        diagnostics.append(
            Diagnostic(
                code=_CODE_API_UNREACHABLE,
                message=(f"Cannot verify destruction of {vm_name} — Azure ML API unreachable"),
                file_path=log_file,
            ),
        )
    elif state in _AZURE_ACTIVE_STATES:
        diagnostics.append(
            Diagnostic(
                code=_CODE_INSTANCE_STILL_ACTIVE,
                message=(f"VM {vm_name} has destroyed_at but Azure ML reports state '{state}'"),
                file_path=log_file,
                detail=("destroyed_at may be incorrect — verify and stop the VM"),
            ),
        )


def _check_machine_nebius(
    *,
    instance_id: str,
    destroyed_at: object,
    log_file: Path,
    diagnostics: list[Diagnostic],
) -> None:
    if destroyed_at is None:
        diagnostics.append(
            Diagnostic(
                code=_CODE_NO_DESTROYED_AT,
                message=(f"Machine {instance_id} has no destroyed_at timestamp"),
                file_path=log_file,
                detail=("Nebius instance may still be running and incurring charges"),
            ),
        )
        state: str | None = _check_nebius_instance_state(instance_id=instance_id)
        if state is not None and state in _NEBIUS_ACTIVE_STATES:
            diagnostics.append(
                Diagnostic(
                    code=_CODE_INSTANCE_STILL_ACTIVE,
                    message=(f"Nebius confirms instance {instance_id} is still in state '{state}'"),
                    file_path=log_file,
                    detail=("Delete this instance immediately to stop charges"),
                ),
            )
        return

    state = _check_nebius_instance_state(instance_id=instance_id)
    if state is None:
        # API unreachable or instance already gone — treat destroyed_at as authoritative.
        diagnostics.append(
            Diagnostic(
                code=_CODE_API_UNREACHABLE,
                message=(f"Cannot verify destruction of {instance_id} — Nebius API unreachable"),
                file_path=log_file,
            ),
        )
    elif state in _NEBIUS_ACTIVE_STATES:
        diagnostics.append(
            Diagnostic(
                code=_CODE_INSTANCE_STILL_ACTIVE,
                message=(
                    f"Instance {instance_id} has destroyed_at but Nebius reports state '{state}'"
                ),
                file_path=log_file,
                detail=("destroyed_at may be incorrect — verify and delete the instance"),
            ),
        )


# ---------------------------------------------------------------------------
# Main verification
# ---------------------------------------------------------------------------


def verify_machines_destroyed(*, task_id: str) -> VerificationResult:
    rm_path: Path = remote_machines_path(task_id=task_id)
    diagnostics: list[Diagnostic] = []

    if not rm_path.exists():
        return VerificationResult(file_path=rm_path, diagnostics=diagnostics)

    try:
        rm_raw: Any = json.loads(rm_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return VerificationResult(file_path=rm_path, diagnostics=diagnostics)

    if not isinstance(rm_raw, list) or len(rm_raw) == 0:
        return VerificationResult(file_path=rm_path, diagnostics=diagnostics)

    rm_data: list[Any] = rm_raw

    # Find machine_log.json for detailed checks
    machine_log_path: Path | None = _find_machine_log(task_id=task_id)
    machine_log_entries: list[dict[str, Any]] = []

    if machine_log_path is not None:
        try:
            raw: Any = json.loads(
                machine_log_path.read_text(encoding="utf-8"),
            )
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict):
                        machine_log_entries.append(entry)
        except (json.JSONDecodeError, OSError):
            diagnostics.append(
                Diagnostic(
                    code=_CODE_MACHINE_LOG_INVALID,
                    message="machine_log.json exists but is not valid JSON",
                    file_path=machine_log_path,
                ),
            )

    # Check each machine entry
    log_file: Path = machine_log_path or rm_path
    for entry in machine_log_entries:
        _check_machine(
            entry=entry,
            log_file=log_file,
            diagnostics=diagnostics,
        )

        instance_id_str: str = str(
            entry.get(_FIELD_INSTANCE_ID, _UNKNOWN_ID),
        )

        # RM-E004: required fields
        for req_field in _REQUIRED_MACHINE_LOG_FIELDS:
            if req_field not in entry:
                diagnostics.append(
                    Diagnostic(
                        code=_CODE_MISSING_FIELD,
                        message=(f"Machine {instance_id_str} missing required field '{req_field}'"),
                        file_path=log_file,
                    ),
                )

        # RM-W003: long running
        duration: object = entry.get("total_duration_hours")
        if isinstance(duration, int | float) and duration > _MAX_DURATION_HOURS:
            diagnostics.append(
                Diagnostic(
                    code=_CODE_LONG_RUNNING,
                    message=(
                        f"Machine {instance_id_str} ran for"
                        f" {duration:.1f}h (>{_MAX_DURATION_HOURS}h)"
                    ),
                    file_path=log_file,
                ),
            )

        # RM-W004: rationale too short
        rationale: object = entry.get("selection_rationale")
        if not isinstance(rationale, str) or len(rationale) < _MIN_RATIONALE_LENGTH:
            diagnostics.append(
                Diagnostic(
                    code=_CODE_RATIONALE_TOO_SHORT,
                    message=(
                        f"Machine {instance_id_str}"
                        " selection_rationale is missing or"
                        f" under {_MIN_RATIONALE_LENGTH} chars"
                    ),
                    file_path=log_file,
                ),
            )

        # RM-W005: failed_attempts validation
        failed_attempts: object = entry.get("failed_attempts")
        if isinstance(failed_attempts, list):
            for idx, attempt in enumerate(failed_attempts):
                if not isinstance(attempt, dict):
                    continue
                missing_fields: list[str] = [
                    f for f in _FAILED_ATTEMPT_REQUIRED_FIELDS if f not in attempt
                ]
                if len(missing_fields) > 0:
                    diagnostics.append(
                        Diagnostic(
                            code=_CODE_FAILED_ATTEMPT_INCOMPLETE,
                            message=(
                                f"Machine {instance_id_str}"
                                f" failed_attempts[{idx}] missing:"
                                f" {', '.join(missing_fields)}"
                            ),
                            file_path=log_file,
                        ),
                    )

        # RM-W006: checkpoint_path for long jobs
        checkpoint_path: object = entry.get("checkpoint_path")
        if (
            isinstance(duration, int | float)
            and duration > _CHECKPOINT_DURATION_THRESHOLD
            and (checkpoint_path is None or checkpoint_path == "")
        ):
            diagnostics.append(
                Diagnostic(
                    code=_CODE_NO_CHECKPOINT_PATH,
                    message=(
                        f"Machine {instance_id_str} ran"
                        f" {duration:.1f}h but no"
                        " checkpoint_path is set"
                    ),
                    file_path=log_file,
                ),
            )

    # RM-E005: cross-reference instance IDs
    rm_machine_ids: set[str] = set()
    for rm_entry in rm_data:
        if isinstance(rm_entry, dict):
            mid: object = rm_entry.get(_FIELD_MACHINE_ID)
            if mid is not None:
                rm_machine_ids.add(str(mid))

    if len(rm_machine_ids) > 0:
        for entry in machine_log_entries:
            iid: str = str(
                entry.get(_FIELD_INSTANCE_ID, _UNKNOWN_ID),
            )
            if iid != _UNKNOWN_ID and iid not in rm_machine_ids:
                diagnostics.append(
                    Diagnostic(
                        code=_CODE_INSTANCE_ID_MISMATCH,
                        message=(
                            f"machine_log instance {iid} not found in remote_machines_used.json"
                        ),
                        file_path=log_file,
                    ),
                )

    # RM-E006: cost comparison
    rm_cost_map: dict[str, float] = {}
    for rm_entry in rm_data:
        if isinstance(rm_entry, dict):
            mid_val: object = rm_entry.get(_FIELD_MACHINE_ID)
            cost_val: object = rm_entry.get("cost_usd")
            if mid_val is not None and isinstance(cost_val, int | float):
                rm_cost_map[str(mid_val)] = float(cost_val)

    for entry in machine_log_entries:
        iid = str(entry.get(_FIELD_INSTANCE_ID, _UNKNOWN_ID))
        log_cost: object = entry.get("total_cost_usd")
        if iid in rm_cost_map and isinstance(log_cost, int | float):
            delta: float = abs(float(log_cost) - rm_cost_map[iid])
            if delta > _COST_TOLERANCE:
                diagnostics.append(
                    Diagnostic(
                        code=_CODE_COST_MISMATCH,
                        message=(
                            f"Machine {iid}: machine_log cost"
                            f" ${log_cost} != remote_machines"
                            f" cost ${rm_cost_map[iid]}"
                        ),
                        file_path=log_file,
                    ),
                )

    # RM-W002: cost vs plan estimate
    if len(machine_log_entries) > 0:
        total_actual: float = sum(
            float(e.get("total_cost_usd", 0))
            for e in machine_log_entries
            if isinstance(e.get("total_cost_usd"), int | float)
        )
        plan_file: Path = plan_path(task_id=task_id)
        if plan_file.exists():
            try:
                plan_text: str = plan_file.read_text(encoding="utf-8")
                matches: list[str] = _DOLLAR_PATTERN.findall(
                    plan_text,
                )
                if len(matches) > 0:
                    estimate: float = max(float(m) for m in matches)
                    if estimate > 0 and total_actual > estimate * _COST_OVERRUN_FACTOR:
                        diagnostics.append(
                            Diagnostic(
                                code=_CODE_COST_EXCEEDS_ESTIMATE,
                                message=(
                                    f"Actual GPU cost ${total_actual:.2f}"
                                    f" exceeds plan estimate"
                                    f" ${estimate:.2f} by >50%"
                                ),
                                file_path=log_file,
                            ),
                        )
            except (OSError, UnicodeDecodeError):
                pass

    # If no machine_log but remote_machines_used.json is non-empty,
    # do a best-effort API check using machine_id from results
    if len(machine_log_entries) == 0 and len(rm_data) > 0:
        for rm_entry in rm_data:
            if not isinstance(rm_entry, dict):
                continue
            machine_id_str: str = str(
                rm_entry.get(_FIELD_MACHINE_ID, _UNKNOWN_ID),
            )
            api_status = _check_instance_via_api(
                instance_id=machine_id_str,
            )
            if api_status is not None and api_status in _ACTIVE_STATUSES:
                diagnostics.append(
                    Diagnostic(
                        code=_CODE_INSTANCE_STILL_ACTIVE,
                        message=(
                            f"Vast.ai confirms instance {machine_id_str} is still {api_status}"
                        ),
                        file_path=rm_path,
                        detail=("Destroy this instance immediately to stop charges"),
                    ),
                )

    return VerificationResult(file_path=rm_path, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Verify all remote machines for a task have been destroyed.",
    )
    group: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group(
        required=True,
    )
    group.add_argument(
        "task_id",
        nargs="?",
        help="Task ID to verify (e.g., 0011-train-baseline-bert)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Verify all tasks",
    )
    return parser.parse_args()


def main() -> None:
    args: argparse.Namespace = _parse_args()

    if args.all:
        task_ids: list[str] = sorted(
            d.name for d in TASKS_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")
        )
    else:
        task_ids = [args.task_id]

    any_errors: bool = False
    for task_id in task_ids:
        result: VerificationResult = verify_machines_destroyed(task_id=task_id)
        if len(result.diagnostics) > 0:
            print_verification_result(result=result)
        if not result.passed:
            any_errors = True

    if any_errors:
        sys.exit(1)
    elif args.all:
        print("All tasks passed machine destruction verification.")


if __name__ == "__main__":
    main()
