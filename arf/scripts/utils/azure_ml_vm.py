"""Azure ML compute instance provisioner accessed via SSH.

Replaces the per-task vast.ai provisioning model with a small fixed pool of
long-lived Azure ML compute instances (currently FT-NC80-v3 and FT-NC80-v2).
The pool is shared with the finetuning team; coordination is by Slack.

Public surface:

* ``acquire(task_id)`` — pick a VM, start it if needed, verify SSH, place a
  per-task lock file on the VM. Refuses if a different task already holds a
  lock on every VM in the pool.
* ``run(task_id, command)`` — execute a shell command on the locked VM with
  periodic heartbeats and (for long jobs) checkpoint reminders.
* ``teardown(task_id, deallocate=True)`` — clear the lock, kill stray vLLM
  processes started by the task, and stop the VM if no other tasks hold
  locks on it.

Also exposes a CLI:

    uv run python -m arf.scripts.utils.azure_ml_vm acquire <task_id>
    uv run python -m arf.scripts.utils.azure_ml_vm teardown <task_id>
    uv run python -m arf.scripts.utils.azure_ml_vm run <task_id> -- <cmd>

All subprocess work goes through two thin shims (``_run_az`` and ``_run_ssh``)
that tests patch with monkeypatch so the module is exercised end-to-end
without ever touching the Azure or SSH control planes.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POOL_CONFIG_PATH: Path = Path(__file__).resolve().parents[3] / "project" / "azure_vm.json"
LOCK_DIR_REMOTE: str = "~/.arf-locks"

PROVIDER: str = "azure-ml"
GPU_MODEL: str = "H100 80GB"
GPU_COUNT: int = 2
SELECTED_OFFER_GPU_DISPLAY: str = "2xH100"

VM_START_TIMEOUT_SECONDS: float = 8 * 60.0
VM_START_POLL_INTERVAL_SECONDS: float = 15.0
SSH_POLL_INTERVAL_SECONDS: float = 10.0
SSH_HANDSHAKE_TIMEOUT_SECONDS: float = 10.0
AZ_TIMEOUT_SECONDS: float = 60.0

HEARTBEAT_INTERVAL_SECONDS: float = 5 * 60.0
CHECKPOINT_INTERVAL_SECONDS: float = 30 * 60.0
LONG_JOB_THRESHOLD_SECONDS: float = 2 * 60 * 60.0

EXIT_OK: int = 0
EXIT_GENERIC_ERROR: int = 1
EXIT_POOL_BUSY: int = 75  # matches sysexits.h EX_TEMPFAIL

# VM-side guarantees checked before the task lock is placed. See the script header
# and LESSONS.md Lessons 10 (persistent storage) and 11 (systemd lingering).
PREFLIGHT_SCRIPT_PATH: Path = Path(__file__).resolve().parent / "remote_preflight.sh"
PREFLIGHT_FAILURE_PHASE: str = "preflight"
PREFLIGHT_TIMEOUT_SECONDS: float = 120.0
# The wire format the script prints. Kept as literals here and in the script rather
# than shared through a constant neither side can see, so a rename on one side shows
# up as a failure instead of staying self-consistently green.
PREFLIGHT_LINGER_ENABLED_KEY: str = "linger_enabled"
PREFLIGHT_PERSIST_PATH_KEY: str = "persist_path"
PREFLIGHT_PERSIST_WRITABLE_KEY: str = "persist_writable"

# Azure compute instance states we care about, normalized to lowercase.
_STATE_RUNNING: str = "running"
_STATE_STOPPED: str = "stopped"
_STATE_STARTING: str = "starting"
_STATE_STOPPING: str = "stopping"
_STATE_UNKNOWN: str = "unknown"


# ---------------------------------------------------------------------------
# Pool config (Pydantic at the I/O edge)
# ---------------------------------------------------------------------------


class VmPoolEntryFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    workspace: str
    resource_group: str
    ssh_host_alias: str
    hourly_cost_usd: float
    priority: int
    notes: str


class VmPoolFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: str
    vms: list[VmPoolEntryFile]


@dataclass(frozen=True, slots=True)
class VmPoolEntry:
    name: str
    workspace: str
    resource_group: str
    ssh_host_alias: str
    hourly_cost_usd: float
    priority: int
    notes: str


def load_pool(*, config_path: Path | None = None) -> list[VmPoolEntry]:
    resolved: Path = config_path if config_path is not None else POOL_CONFIG_PATH
    pool_file: VmPoolFile = VmPoolFile.model_validate_json(
        resolved.read_text(encoding="utf-8"),
    )
    entries: list[VmPoolEntry] = [
        VmPoolEntry(
            name=v.name,
            workspace=v.workspace,
            resource_group=v.resource_group,
            ssh_host_alias=v.ssh_host_alias,
            hourly_cost_usd=v.hourly_cost_usd,
            priority=v.priority,
            notes=v.notes,
        )
        for v in pool_file.vms
    ]
    entries.sort(key=lambda e: e.priority)
    return entries


# ---------------------------------------------------------------------------
# Result dataclasses (internal, returned to callers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    vm_name: str
    failure_reason: str
    failure_phase: str
    duration_seconds: float
    wasted_cost_usd: float
    timestamp: str


@dataclass(frozen=True, slots=True)
class AcquireResult:
    task_id: str
    vm: VmPoolEntry
    acquired_at: str
    search_started_at: str
    ready_at: str
    total_provisioning_seconds: float
    started_vm: bool
    failed_attempts: list[FailedAttempt] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TeardownResult:
    task_id: str
    vm_name: str
    deallocated: bool
    other_locks_present: bool
    destroyed_at: str
    duration_hours: float
    total_cost_usd: float


@dataclass(frozen=True, slots=True)
class RunResult:
    task_id: str
    vm_name: str
    exit_code: int
    duration_seconds: float
    heartbeats: int


# ---------------------------------------------------------------------------
# Subprocess shims (the only places that touch the outside world)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _run_az(*, args: list[str], timeout: float = AZ_TIMEOUT_SECONDS) -> CommandResult:
    full: list[str] = ["az", *args]
    proc: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
        full,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _run_ssh(
    *,
    host_alias: str,
    remote_command: str,
    timeout: float = AZ_TIMEOUT_SECONDS,
    connect_timeout: float = SSH_HANDSHAKE_TIMEOUT_SECONDS,
) -> CommandResult:
    args: list[str] = [
        "ssh",
        "-o",
        f"ConnectTimeout={int(connect_timeout)}",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        host_alias,
        remote_command,
    ]
    proc: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


# ---------------------------------------------------------------------------
# VM-side preflight guarantees
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreflightResult:
    linger_enabled: bool
    persist_path: str
    persist_writable: bool


def _parse_preflight_stdout(*, stdout: str) -> PreflightResult | None:
    # The script prints one JSON line, but a login shell may print a banner or a
    # motd first, so scan for the line that parses rather than assuming position.
    for line in stdout.splitlines():
        stripped: str = line.strip()
        if len(stripped) == 0:
            continue
        try:
            payload: object = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        linger: object = payload.get(PREFLIGHT_LINGER_ENABLED_KEY)
        persist_path: object = payload.get(PREFLIGHT_PERSIST_PATH_KEY)
        writable: object = payload.get(PREFLIGHT_PERSIST_WRITABLE_KEY)
        if not isinstance(linger, bool):
            continue
        if not isinstance(persist_path, str):
            continue
        if not isinstance(writable, bool):
            continue
        return PreflightResult(
            linger_enabled=linger,
            persist_path=persist_path,
            persist_writable=writable,
        )
    return None


def run_remote_preflight(*, vm: VmPoolEntry) -> PreflightResult | None:
    """Run the VM-side guarantees on ``vm``; ``None`` means the box must not be used.

    Ships ``remote_preflight.sh`` as the SSH command itself, so nothing has to be
    staged on the box first. A ``None`` here is not a warning to log and continue
    past: the two guarantees it checks are the ones whose absence destroys a
    training run silently (``LESSONS.md`` Lessons 10 and 11).
    """

    # A sibling data file, not an import: nothing fails at build time if it is renamed,
    # and an uncaught read error here would abort the whole pool instead of one VM.
    assert PREFLIGHT_SCRIPT_PATH.is_file(), f"preflight script exists at {PREFLIGHT_SCRIPT_PATH}"
    script: str = PREFLIGHT_SCRIPT_PATH.read_text(encoding="utf-8")
    try:
        result: CommandResult = _run_ssh(
            host_alias=vm.ssh_host_alias,
            remote_command=script,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"preflight on {vm.name} timed out after {PREFLIGHT_TIMEOUT_SECONDS:.0f}s\n",
        )
        return None
    if result.returncode != 0:
        # The script names which guarantee failed, on which path, for which user. Put it
        # where run_with_logs will capture it — the FailedAttempt recorded upstream can
        # only say that preflight failed, not why.
        sys.stderr.write(f"preflight failed on {vm.name}: {result.stderr.strip()}\n")
        return None
    parsed: PreflightResult | None = _parse_preflight_stdout(stdout=result.stdout)
    if parsed is None:
        return None
    # Enforce the guarantees on this side too, rather than trusting the exit code to
    # agree with the payload. A detector that reads fields nothing ever varies is how the
    # first liveness bug survived for months (LESSONS Lesson 8, first follow-up).
    if not parsed.linger_enabled or not parsed.persist_writable:
        sys.stderr.write(
            f"preflight on {vm.name} reported linger_enabled={parsed.linger_enabled} "
            f"persist_writable={parsed.persist_writable}\n",
        )
        return None
    return parsed


# ---------------------------------------------------------------------------
# Azure compute control
# ---------------------------------------------------------------------------


def _normalize_state(*, raw: str | None) -> str:
    if raw is None:
        return _STATE_UNKNOWN
    lowered: str = raw.strip().lower()
    if lowered in {"running"}:
        return _STATE_RUNNING
    if lowered in {"stopped", "deallocated"}:
        return _STATE_STOPPED
    if lowered in {"starting", "creating"}:
        return _STATE_STARTING
    if lowered in {"stopping", "deallocating"}:
        return _STATE_STOPPING
    return _STATE_UNKNOWN


def get_compute_state(*, vm: VmPoolEntry) -> str:
    result: CommandResult = _run_az(
        args=[
            "ml",
            "compute",
            "show",
            "--name",
            vm.name,
            "--workspace-name",
            vm.workspace,
            "--resource-group",
            vm.resource_group,
            "-o",
            "json",
        ],
    )
    if result.returncode != 0:
        return _STATE_UNKNOWN
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _STATE_UNKNOWN
    if not isinstance(payload, dict):
        return _STATE_UNKNOWN
    raw_state: object = payload.get("state")
    if not isinstance(raw_state, str):
        return _STATE_UNKNOWN
    return _normalize_state(raw=raw_state)


def start_compute(*, vm: VmPoolEntry) -> CommandResult:
    return _run_az(
        args=[
            "ml",
            "compute",
            "start",
            "--name",
            vm.name,
            "--workspace-name",
            vm.workspace,
            "--resource-group",
            vm.resource_group,
            "--no-wait",
        ],
    )


def stop_compute(*, vm: VmPoolEntry) -> CommandResult:
    return _run_az(
        args=[
            "ml",
            "compute",
            "stop",
            "--name",
            vm.name,
            "--workspace-name",
            vm.workspace,
            "--resource-group",
            vm.resource_group,
            "--no-wait",
        ],
    )


# ---------------------------------------------------------------------------
# SSH-level operations
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _now_monotonic() -> float:
    return time.monotonic()


def _ssh_ok(*, vm: VmPoolEntry) -> bool:
    result: CommandResult = _run_ssh(
        host_alias=vm.ssh_host_alias,
        remote_command="true",
        timeout=SSH_HANDSHAKE_TIMEOUT_SECONDS + 5.0,
        connect_timeout=SSH_HANDSHAKE_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def _wait_for_ssh(*, vm: VmPoolEntry, deadline: float) -> bool:
    while _now_monotonic() < deadline:
        if _ssh_ok(vm=vm):
            return True
        time.sleep(SSH_POLL_INTERVAL_SECONDS)
    return False


def _wait_for_state(
    *,
    vm: VmPoolEntry,
    target: str,
    deadline: float,
) -> bool:
    while _now_monotonic() < deadline:
        state: str = get_compute_state(vm=vm)
        if state == target:
            return True
        time.sleep(VM_START_POLL_INTERVAL_SECONDS)
    return False


def _lock_path(*, task_id: str) -> str:
    return f"{LOCK_DIR_REMOTE}/{task_id}.lock"


def list_remote_locks(*, vm: VmPoolEntry) -> list[str]:
    result: CommandResult = _run_ssh(
        host_alias=vm.ssh_host_alias,
        remote_command=(
            f"mkdir -p {LOCK_DIR_REMOTE} && "
            f"ls {LOCK_DIR_REMOTE}/ 2>/dev/null | grep '\\.lock$' || true"
        ),
    )
    if result.returncode != 0:
        return []
    locks: list[str] = []
    for line in result.stdout.splitlines():
        name: str = line.strip()
        if name.endswith(".lock"):
            locks.append(name[: -len(".lock")])
    return locks


def place_remote_lock(*, vm: VmPoolEntry, task_id: str) -> None:
    payload: dict[str, str] = {
        "task_id": task_id,
        "acquired_at": _now_iso(),
    }
    body: str = json.dumps(payload)
    quoted_body: str = shlex.quote(body)
    target: str = _lock_path(task_id=task_id)
    remote_cmd: str = f"mkdir -p {LOCK_DIR_REMOTE} && printf '%s' {quoted_body} > {target}"
    result: CommandResult = _run_ssh(host_alias=vm.ssh_host_alias, remote_command=remote_cmd)
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to place lock on {vm.name}: {result.stderr.strip() or result.stdout.strip()}",
        )


def clear_remote_lock(*, vm: VmPoolEntry, task_id: str) -> bool:
    target: str = _lock_path(task_id=task_id)
    result: CommandResult = _run_ssh(
        host_alias=vm.ssh_host_alias,
        remote_command=f"rm -f {target}",
    )
    return result.returncode == 0


def kill_task_vllm_processes(*, vm: VmPoolEntry, task_id: str) -> CommandResult:
    # Match vLLM processes tagged with the task_id in their command line. The
    # task is expected to launch vLLM with --served-model-name or env containing
    # the task_id; pkill -f is best-effort, never fatal.
    return _run_ssh(
        host_alias=vm.ssh_host_alias,
        remote_command=(f"pkill -f 'vllm.*{shlex.quote(task_id)}' 2>/dev/null; true"),
    )


# ---------------------------------------------------------------------------
# Acquire / Run / Teardown
# ---------------------------------------------------------------------------


class PoolBusyError(RuntimeError):
    """Raised when no VM in the pool can be acquired."""


def _try_acquire_one(
    *,
    vm: VmPoolEntry,
    task_id: str,
) -> tuple[bool, FailedAttempt | None, bool]:
    """Attempt to acquire ``vm`` for ``task_id``, releasing it if abandoned.

    Wraps ``_attempt_acquire_one``: when that attempt started ``vm`` itself
    but did not end up acquiring it, this stops the VM again before
    returning, so a failed acquire never leaves a billing VM for the caller
    to notice and stop by hand.
    """
    acquired, failure, started_vm = _attempt_acquire_one(vm=vm, task_id=task_id)
    if not acquired and started_vm:
        stop_result: CommandResult = stop_compute(vm=vm)
        if stop_result.returncode != 0:
            sys.stderr.write(
                f"failed to stop {vm.name} after abandoning a failed acquire attempt: "
                f"{stop_result.stderr.strip() or stop_result.stdout.strip()}\n",
            )
    return acquired, failure, started_vm


def _attempt_acquire_one(
    *,
    vm: VmPoolEntry,
    task_id: str,
) -> tuple[bool, FailedAttempt | None, bool]:
    """Attempt to acquire ``vm`` for ``task_id``.

    Returns ``(acquired, failure_record, started_vm)``. When ``acquired`` is
    False, ``failure_record`` describes the reason; ``started_vm`` reports
    whether this attempt issued an az start call (used so the caller can
    compute wasted cost on giving up, and so ``_try_acquire_one`` knows
    whether to release the VM it started).
    """
    attempt_start: float = _now_monotonic()
    timestamp: str = _now_iso()
    state: str = get_compute_state(vm=vm)
    started_vm: bool = False

    if state == _STATE_STOPPING:
        return (
            False,
            FailedAttempt(
                vm_name=vm.name,
                failure_reason=f"VM {vm.name} is stopping; will not race",
                failure_phase="az_show",
                duration_seconds=_now_monotonic() - attempt_start,
                wasted_cost_usd=0.0,
                timestamp=timestamp,
            ),
            False,
        )

    if state == _STATE_STOPPED:
        start_result: CommandResult = start_compute(vm=vm)
        started_vm = True
        if start_result.returncode != 0:
            return (
                False,
                FailedAttempt(
                    vm_name=vm.name,
                    failure_reason=(
                        f"az ml compute start failed: "
                        f"{start_result.stderr.strip() or start_result.stdout.strip()}"
                    ),
                    failure_phase="az_start",
                    duration_seconds=_now_monotonic() - attempt_start,
                    wasted_cost_usd=0.0,
                    timestamp=timestamp,
                ),
                True,
            )

    deadline: float = _now_monotonic() + VM_START_TIMEOUT_SECONDS
    if state != _STATE_RUNNING and not _wait_for_state(
        vm=vm, target=_STATE_RUNNING, deadline=deadline
    ):
        return (
            False,
            FailedAttempt(
                vm_name=vm.name,
                failure_reason=(
                    f"VM {vm.name} did not reach running state within "
                    f"{int(VM_START_TIMEOUT_SECONDS)}s"
                ),
                failure_phase="vm_start_timeout",
                duration_seconds=_now_monotonic() - attempt_start,
                wasted_cost_usd=_estimate_wasted_cost(
                    hourly=vm.hourly_cost_usd,
                    seconds=_now_monotonic() - attempt_start,
                ),
                timestamp=timestamp,
            ),
            started_vm,
        )

    if not _wait_for_ssh(vm=vm, deadline=deadline):
        return (
            False,
            FailedAttempt(
                vm_name=vm.name,
                failure_reason=f"SSH did not come up on {vm.name} within deadline",
                failure_phase="ssh_connect",
                duration_seconds=_now_monotonic() - attempt_start,
                wasted_cost_usd=_estimate_wasted_cost(
                    hourly=vm.hourly_cost_usd,
                    seconds=_now_monotonic() - attempt_start,
                ),
                timestamp=timestamp,
            ),
            started_vm,
        )

    existing_locks: list[str] = list_remote_locks(vm=vm)
    foreign_locks: list[str] = [lock for lock in existing_locks if lock != task_id]
    if len(foreign_locks) > 0:
        return (
            False,
            FailedAttempt(
                vm_name=vm.name,
                failure_reason=(f"VM {vm.name} already locked by: {', '.join(foreign_locks)}"),
                failure_phase="lock_held",
                duration_seconds=_now_monotonic() - attempt_start,
                wasted_cost_usd=0.0,
                timestamp=timestamp,
            ),
            started_vm,
        )

    # Before the lock, not after: a locked VM is one this task is committed to, and
    # a box that fails preflight is one whose training job dies on disconnect or
    # whose checkpoints land on an ephemeral disk. Failing here lets the pool loop
    # fall through to the next entry instead of stranding the task on it.
    if run_remote_preflight(vm=vm) is None:
        return (
            False,
            FailedAttempt(
                vm_name=vm.name,
                failure_reason=(
                    f"VM {vm.name} failed remote preflight (systemd lingering or the "
                    f"persistent-storage mount); see the step log for the script's stderr"
                ),
                failure_phase=PREFLIGHT_FAILURE_PHASE,
                duration_seconds=_now_monotonic() - attempt_start,
                wasted_cost_usd=_estimate_wasted_cost(
                    hourly=vm.hourly_cost_usd,
                    seconds=_now_monotonic() - attempt_start,
                ),
                timestamp=timestamp,
            ),
            started_vm,
        )

    place_remote_lock(vm=vm, task_id=task_id)
    return (True, None, started_vm)


def _estimate_wasted_cost(*, hourly: float, seconds: float) -> float:
    return hourly * (seconds / 3600.0)


def _find_pool_vm(*, pool: list[VmPoolEntry], vm_name: str) -> VmPoolEntry:
    matched: list[VmPoolEntry] = [vm for vm in pool if vm.name == vm_name]
    if len(matched) == 0:
        available: str = ", ".join(vm.name for vm in pool)
        raise RuntimeError(
            f"--vm-name {vm_name!r} does not match any VM in pool (available: {available})"
        )
    return matched[0]


def acquire(
    *,
    task_id: str,
    pool: list[VmPoolEntry] | None = None,
    intervention_dir: Path | None = None,
    vm_name: str | None = None,
) -> AcquireResult:
    pool_to_use: list[VmPoolEntry] = pool if pool is not None else load_pool()
    if len(pool_to_use) == 0:
        raise RuntimeError("VM pool is empty; check project/azure_vm.json")

    if vm_name is not None:
        pool_to_use = [_find_pool_vm(pool=pool_to_use, vm_name=vm_name)]

    search_started_at: str = _now_iso()
    search_started_monotonic: float = _now_monotonic()
    failed: list[FailedAttempt] = []

    for vm in pool_to_use:
        acquired, failure, started_vm = _try_acquire_one(vm=vm, task_id=task_id)
        if acquired:
            ready_at: str = _now_iso()
            total: float = _now_monotonic() - search_started_monotonic
            return AcquireResult(
                task_id=task_id,
                vm=vm,
                acquired_at=ready_at,
                search_started_at=search_started_at,
                ready_at=ready_at,
                total_provisioning_seconds=total,
                started_vm=started_vm,
                failed_attempts=failed,
            )
        if failure is not None:
            failed.append(failure)

    _write_pool_busy_intervention(
        task_id=task_id,
        failed=failed,
        intervention_dir=intervention_dir,
    )
    summary: str = "; ".join(f"{f.vm_name}: {f.failure_reason}" for f in failed)
    raise PoolBusyError(f"all VMs in pool unavailable -> {summary}")


def _pool_busy_filename(*, failed: list[FailedAttempt]) -> str:
    # Keyed on the attempted VM name(s) so that parallel subagents provisioning different
    # --vm-name-pinned VMs for the same task_id each get their own intervention file instead of
    # overwriting one another's (see t0055's intervention/pool_busy.md note).
    vm_slugs: list[str] = sorted({f.vm_name.lower() for f in failed})
    if len(vm_slugs) == 0:
        return "pool_busy.md"
    return f"pool_busy_{'_'.join(vm_slugs)}.md"


def _write_pool_busy_intervention(
    *,
    task_id: str,
    failed: list[FailedAttempt],
    intervention_dir: Path | None,
) -> None:
    base: Path
    if intervention_dir is not None:
        base = intervention_dir
    else:
        base = POOL_CONFIG_PATH.parent.parent / "tasks" / task_id / "intervention"
    base.mkdir(parents=True, exist_ok=True)
    path: Path = base / _pool_busy_filename(failed=failed)
    lines: list[str] = [
        "# Azure ML compute pool busy",
        "",
        f"Task `{task_id}` could not acquire a VM from `project/azure_vm.json`.",
        "",
        "## Attempts",
        "",
    ]
    for f in failed:
        lines.append(f"* **{f.vm_name}** ({f.failure_phase}): {f.failure_reason}")
    lines.extend(
        [
            "",
            "## Resolution",
            "",
            "* Check `#finetuning` Slack to see whether the finetuning team is",
            "  using both VMs.",
            "* Run `az ml compute show --name FT-NC80-v3 "
            "--workspace-name finetuning-workspace --resource-group rezolve-AI -o json`",
            "  to confirm state.",
            "* Once a VM is free, re-run the setup-machines step.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run(
    *,
    task_id: str,
    command: str,
    vm: VmPoolEntry | None = None,
    pool: list[VmPoolEntry] | None = None,
    estimated_duration_seconds: float | None = None,
    progress_callback: Any = None,
) -> RunResult:
    """Execute ``command`` on the VM currently locked by ``task_id``.

    Streams nothing — captures stdout/stderr at the end. Heartbeats are
    surfaced via ``progress_callback`` (a ``Callable[[dict[str, Any]], None]``)
    if supplied; the test suite uses this to assert intervals without sleeping.

    Heartbeats fire every 5 minutes; for jobs estimated longer than 2 hours,
    a checkpoint reminder fires every 30 minutes alongside the heartbeat.
    """
    target_vm: VmPoolEntry = _resolve_locked_vm(task_id=task_id, vm=vm, pool=pool)
    started_monotonic: float = _now_monotonic()

    proc: subprocess.Popen[str] = subprocess.Popen(  # noqa: S603
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            target_vm.ssh_host_alias,
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    heartbeats: int = 0
    next_heartbeat: float = started_monotonic + HEARTBEAT_INTERVAL_SECONDS
    next_checkpoint: float = started_monotonic + CHECKPOINT_INTERVAL_SECONDS
    is_long_job: bool = (
        estimated_duration_seconds is not None
        and estimated_duration_seconds > LONG_JOB_THRESHOLD_SECONDS
    )

    while True:
        if proc.poll() is not None:
            break
        now: float = _now_monotonic()
        if now >= next_heartbeat:
            heartbeats += 1
            if progress_callback is not None:
                progress_callback(
                    {
                        "kind": "heartbeat",
                        "elapsed_seconds": now - started_monotonic,
                        "heartbeat_index": heartbeats,
                    },
                )
            next_heartbeat = now + HEARTBEAT_INTERVAL_SECONDS
        if is_long_job and now >= next_checkpoint:
            if progress_callback is not None:
                progress_callback(
                    {
                        "kind": "checkpoint_reminder",
                        "elapsed_seconds": now - started_monotonic,
                    },
                )
            next_checkpoint = now + CHECKPOINT_INTERVAL_SECONDS
        time.sleep(1.0)

    proc.wait()
    duration: float = _now_monotonic() - started_monotonic
    return RunResult(
        task_id=task_id,
        vm_name=target_vm.name,
        exit_code=proc.returncode,
        duration_seconds=duration,
        heartbeats=heartbeats,
    )


def _resolve_locked_vm(
    *,
    task_id: str,
    vm: VmPoolEntry | None,
    pool: list[VmPoolEntry] | None,
) -> VmPoolEntry:
    if vm is not None:
        return vm
    pool_to_use: list[VmPoolEntry] = pool if pool is not None else load_pool()
    for candidate in pool_to_use:
        if task_id in list_remote_locks(vm=candidate):
            return candidate
    raise RuntimeError(
        f"no VM in the pool currently holds a lock for task {task_id}",
    )


def teardown(
    *,
    task_id: str,
    deallocate: bool = True,
    vm: VmPoolEntry | None = None,
    pool: list[VmPoolEntry] | None = None,
    acquired_at: str | None = None,
) -> TeardownResult:
    target_vm: VmPoolEntry = _resolve_locked_vm(task_id=task_id, vm=vm, pool=pool)

    # Clearing the on-VM lock file needs SSH, which needs the VM running. A VM stopped outside
    # azure_ml_vm (by hand, or by the idle watchdog) must still be releasable through this one
    # function -- a hand-stopped VM's stale lock cannot otherwise be cleared and blocks every
    # later acquire attempt against it.
    started_from_stopped: bool = get_compute_state(vm=target_vm) == _STATE_STOPPED
    if started_from_stopped:
        start_compute(vm=target_vm)
        start_deadline: float = _now_monotonic() + VM_START_TIMEOUT_SECONDS
        started_ok: bool = _wait_for_state(
            vm=target_vm,
            target=_STATE_RUNNING,
            deadline=start_deadline,
        ) and _wait_for_ssh(vm=target_vm, deadline=start_deadline)
        if not started_ok:
            raise RuntimeError(
                f"could not start {target_vm.name} to release task {task_id}'s lock; the VM "
                "must be reachable over SSH to clear the on-VM lock file",
            )

    if not clear_remote_lock(vm=target_vm, task_id=task_id):
        raise RuntimeError(
            f"failed to clear the lock for {task_id} on {target_vm.name}; the on-VM lock file "
            "may still be present -- do not treat this task's VM as released",
        )
    kill_task_vllm_processes(vm=target_vm, task_id=task_id)

    other_locks: list[str] = [lock for lock in list_remote_locks(vm=target_vm) if lock != task_id]
    other_locks_present: bool = len(other_locks) > 0

    # Restore the VM to how we found it: if this call started it just to reach it over SSH,
    # stop it again even when a sibling lock is present -- a sibling lock on a VM that was
    # stopped is necessarily stale (nobody can be actively using a stopped VM), so it never
    # protects a live task the way it does when the VM was already running. `deallocate=False`
    # (the `--keep-running` override) still wins over restoring the original state, since it is
    # an explicit request to leave the VM up.
    deallocated: bool = False
    if deallocate and (not other_locks_present or started_from_stopped):
        stop_result: CommandResult = stop_compute(vm=target_vm)
        deallocated = stop_result.returncode == 0

    destroyed_at: str = _now_iso()
    duration_hours: float = 0.0
    if acquired_at is not None:
        try:
            start_dt: datetime = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
            end_dt: datetime = datetime.fromisoformat(destroyed_at.replace("Z", "+00:00"))
            duration_hours = (end_dt - start_dt).total_seconds() / 3600.0
        except ValueError:
            duration_hours = 0.0
    total_cost_usd: float = target_vm.hourly_cost_usd * duration_hours

    return TeardownResult(
        task_id=task_id,
        vm_name=target_vm.name,
        deallocated=deallocated,
        other_locks_present=other_locks_present,
        destroyed_at=destroyed_at,
        duration_hours=duration_hours,
        total_cost_usd=total_cost_usd,
    )


# ---------------------------------------------------------------------------
# machine_log.json shaping
# ---------------------------------------------------------------------------


def to_machine_log_entry(
    *,
    acquire_result: AcquireResult,
    teardown_result: TeardownResult | None = None,
    image: str = "azure-ml-system-managed",
    disk_gb: int = 0,
    label: str | None = None,
    cuda_version: str = "unknown",
    checkpoint_path: str | None = None,
    heartbeat_path: str | None = None,
) -> dict[str, Any]:
    """Produce a machine_log.json-compatible entry from acquire/teardown results.

    The shape mirrors the vast.ai-era schema (see
    ``arf/specifications/remote_machines_specification.md``) so that
    ``aggregate_machines.py`` continues to work unchanged.
    """
    vm: VmPoolEntry = acquire_result.vm
    entry: dict[str, Any] = {
        "provider": PROVIDER,
        "instance_id": vm.name,
        "offer_id": 0,
        "search_criteria": {
            "gpu_name": GPU_MODEL,
            "num_gpus": GPU_COUNT,
            "min_gpu_ram": 80.0,
            "min_cpu_ram": None,
            "min_disk": None,
            "min_reliability": 1.0,
            "extra_filters": "azure-ml-pool",
        },
        "selected_offer": {
            "offer_id": 0,
            "gpu": SELECTED_OFFER_GPU_DISPLAY,
            "gpu_count": GPU_COUNT,
            "gpu_ram_gb": 80.0,
            "cpu_ram_gb": 0.0,
            "disk_gb": 0.0,
            "price_per_hour": vm.hourly_cost_usd,
            "reliability": 1.0,
            "location": "azure-eastus2",
        },
        "selection_rationale": (
            f"Acquired {vm.name} from the Azure ML pool (priority {vm.priority}) "
            f"at ${vm.hourly_cost_usd:.2f}/hr."
        ),
        "image": image,
        "disk_gb": disk_gb,
        "label": label,
        "ssh_host": vm.ssh_host_alias,
        "ssh_port": 0,
        "gpu_verified": SELECTED_OFFER_GPU_DISPLAY,
        "cuda_version": cuda_version,
        "created_at": acquire_result.acquired_at,
        "ready_at": acquire_result.ready_at,
        "destroyed_at": None,
        "total_duration_hours": None,
        "total_cost_usd": None,
        "search_started_at": acquire_result.search_started_at,
        "total_provisioning_seconds": acquire_result.total_provisioning_seconds,
        "failed_attempts": [
            {
                "offer_id": 0,
                "instance_id": f.vm_name,
                "gpu": SELECTED_OFFER_GPU_DISPLAY,
                "failure_reason": f.failure_reason,
                "failure_phase": f.failure_phase,
                "duration_seconds": f.duration_seconds,
                "wasted_cost_usd": f.wasted_cost_usd,
                "timestamp": f.timestamp,
            }
            for f in acquire_result.failed_attempts
        ],
        "checkpoint_path": checkpoint_path,
        "heartbeat_path": heartbeat_path,
    }
    if teardown_result is not None:
        entry["destroyed_at"] = teardown_result.destroyed_at
        entry["total_duration_hours"] = teardown_result.duration_hours
        entry["total_cost_usd"] = teardown_result.total_cost_usd
    return entry


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_acquire(*, result: AcquireResult) -> None:
    payload: dict[str, Any] = {
        "task_id": result.task_id,
        "vm_name": result.vm.name,
        "ssh_host_alias": result.vm.ssh_host_alias,
        "hourly_cost_usd": result.vm.hourly_cost_usd,
        "acquired_at": result.acquired_at,
        "started_vm": result.started_vm,
        "search_started_at": result.search_started_at,
        "total_provisioning_seconds": result.total_provisioning_seconds,
        "failed_attempts": [asdict(f) for f in result.failed_attempts],
    }
    print(json.dumps(payload, indent=2))


def _print_teardown(*, result: TeardownResult) -> None:
    print(json.dumps(asdict(result), indent=2))


def _print_run(*, result: RunResult) -> None:
    print(json.dumps(asdict(result), indent=2))


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="azure_ml_vm",
        description="Azure ML compute instance provisioner via SSH",
    )
    sub: argparse._SubParsersAction[argparse.ArgumentParser] = parser.add_subparsers(
        dest="command",
        required=True,
    )

    acquire_parser: argparse.ArgumentParser = sub.add_parser(
        "acquire",
        help="Acquire a VM from the pool for a task",
    )
    acquire_parser.add_argument("task_id")
    acquire_parser.add_argument(
        "--vm-name",
        type=str,
        default=None,
        help=(
            "Pin acquisition to a single VM in the pool by name. When set, the "
            "command will not roll over to other VMs and will fail with exit 75 "
            "(pool busy) if the named VM is locked or unavailable. Use this when "
            "a task's measurements are statistically valid only on one specific VM "
            "(e.g., paired comparisons against a frozen baseline measured on that VM)."
        ),
    )

    teardown_parser: argparse.ArgumentParser = sub.add_parser(
        "teardown",
        help="Release the VM held by a task",
    )
    teardown_parser.add_argument("task_id")
    teardown_parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Do not deallocate the VM after releasing the lock",
    )
    teardown_parser.add_argument(
        "--acquired-at",
        type=str,
        default=None,
        help="ISO 8601 timestamp from acquire output (used to compute duration)",
    )
    teardown_parser.add_argument(
        "--vm-name",
        type=str,
        default=None,
        help=(
            "Pin release to a single VM in the pool by name, from this task's own acquire "
            "output. Without it, resolving the locked VM walks the pool over SSH, which cannot "
            "find a lock on a VM that is currently stopped (by hand, or by the idle watchdog) "
            "-- pass this when the VM might be stopped."
        ),
    )

    run_parser: argparse.ArgumentParser = sub.add_parser(
        "run",
        help="Execute a shell command on the VM held by a task",
    )
    run_parser.add_argument("task_id")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)

    args: argparse.Namespace = parser.parse_args(argv)

    if args.command == "acquire":
        try:
            result: AcquireResult = acquire(task_id=args.task_id, vm_name=args.vm_name)
        except PoolBusyError as err:
            print(f"pool busy: {err}", file=sys.stderr)
            return EXIT_POOL_BUSY
        except Exception as err:  # noqa: BLE001
            print(f"acquire failed: {err}", file=sys.stderr)
            return EXIT_GENERIC_ERROR
        _print_acquire(result=result)
        return EXIT_OK

    if args.command == "teardown":
        try:
            pinned_vm: VmPoolEntry | None = None
            if args.vm_name is not None:
                pinned_vm = _find_pool_vm(pool=load_pool(), vm_name=args.vm_name)
            t_result: TeardownResult = teardown(
                task_id=args.task_id,
                deallocate=not args.keep_running,
                vm=pinned_vm,
                acquired_at=args.acquired_at,
            )
        except Exception as err:  # noqa: BLE001
            print(f"teardown failed: {err}", file=sys.stderr)
            return EXIT_GENERIC_ERROR
        _print_teardown(result=t_result)
        return EXIT_OK

    if args.command == "run":
        if len(args.command) == 0:
            print("run: a remote command is required after --", file=sys.stderr)
            return EXIT_GENERIC_ERROR
        # argparse.REMAINDER keeps the leading "--" when present.
        tokens: list[str] = (
            args.command[1:] if len(args.command) > 0 and args.command[0] == "--" else args.command
        )
        joined: str = " ".join(shlex.quote(tok) for tok in tokens)
        try:
            r_result: RunResult = run(task_id=args.task_id, command=joined)
        except Exception as err:  # noqa: BLE001
            print(f"run failed: {err}", file=sys.stderr)
            return EXIT_GENERIC_ERROR
        _print_run(result=r_result)
        return r_result.exit_code

    return EXIT_GENERIC_ERROR


if __name__ == "__main__":
    sys.exit(main())
