"""Nebius GPU provisioning utilities.

This module wraps the Nebius CLI (``nebius``) to provide a uniform provider
surface mirroring ``arf.scripts.utils.vast_machines`` and
``arf.scripts.utils.azure_ml_vm``. Each acquire creates a fresh instance from
a platform/preset selection; teardown deletes it. A lock file under
``~/.arf-locks/<task_id>.lock`` protects against accidental concurrent
acquires for the same task (Lesson 8).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arf.scripts.utils.watchdog_provisioning import (
    WatchdogConfig,
    render_nebius_cloud_init,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPEC_VERSION: str = "5"
PROVIDER_SLUG: str = "nebius"

DEFAULT_BOOT_DISK_SIZE_GIBIBYTES: int = 100
DEFAULT_MAX_WAIT_SECONDS: int = 900
DEFAULT_SSH_SMOKE_GATE_TIMEOUT_SECONDS: int = 300
DEFAULT_POLL_INTERVAL_SECONDS: float = 10.0
DEFAULT_SSH_POLL_INTERVAL_SECONDS: float = 5.0
DEFAULT_SSH_TCP_TIMEOUT_SECONDS: float = 3.0
DEFAULT_CLI_TIMEOUT_SECONDS: float = 120.0

QUOTA_TYPE_PREFIX: str = "compute.instance.gpu."


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NebiusProvisioningError(RuntimeError):
    """Raised when Nebius provisioning fails for any reason."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NebiusOffer:
    platform: str
    preset: str
    region: str
    gpu_class: str
    gpu_count: int
    gpu_memory_gibibytes: int
    vcpu_count: int
    ram_gibibytes: int
    hourly_cost_estimate_usd: float | None


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    offer: NebiusOffer
    failure_reason: str
    failed_at: str
    wasted_cost_usd: float


@dataclass(frozen=True, slots=True)
class AcquireResult:
    instance_id: str
    public_ip: str | None
    private_ip: str
    boot_disk_id: str
    region: str
    platform: str
    preset: str
    image_id: str
    acquired_at: str
    hourly_cost_estimate_usd: float | None
    tenant_id: str
    project_id: str


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    acquire_result: AcquireResult
    failed_attempts: list[FailedAttempt]
    total_provisioning_seconds: float


@dataclass(frozen=True, slots=True)
class TeardownResult:
    instance_id: str
    terminated_at: str
    success: bool
    error_message: str | None
    teardown_duration_seconds: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _resolve_lock_dir(*, lock_dir: Path | None) -> Path:
    if lock_dir is None:
        return Path.home() / ".arf-locks"
    return lock_dir


def _lock_path(*, lock_dir: Path, task_id: str) -> Path:
    return lock_dir / f"{task_id}.lock"


def _acquire_lock(*, lock_dir: Path, task_id: str) -> Path:
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path: Path = _lock_path(lock_dir=lock_dir, task_id=task_id)
    if lock_path.exists():
        existing: str = ""
        try:
            existing = lock_path.read_text(encoding="utf-8")
        except OSError:
            existing = "<unreadable>"
        raise NebiusProvisioningError(f"lock file already held by {task_id}: {existing.strip()}")
    contents: str = f"{task_id}\n{_now_iso()}\n{os.getpid()}\n"
    lock_path.write_text(contents, encoding="utf-8")
    return lock_path


def _release_lock(*, lock_dir: Path, task_id: str) -> None:
    lock_path: Path = _lock_path(lock_dir=lock_dir, task_id=task_id)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _run_cli(
    *,
    cmd: list[str],
    timeout_seconds: float = DEFAULT_CLI_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Execute a Nebius CLI command and return the CompletedProcess.

    A thin wrapper around ``subprocess.run`` so tests can monkeypatch the
    ``subprocess.run`` attribute in this module.
    """
    return subprocess.run(  # noqa: S603 - args are constructed from typed inputs
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _derive_gpu_class(
    *,
    quota_type: str | None,
    platform_name: str,
) -> str:
    if quota_type is not None and quota_type.startswith(QUOTA_TYPE_PREFIX):
        suffix: str = quota_type[len(QUOTA_TYPE_PREFIX) :]
        if len(suffix) > 0:
            return suffix.upper()
    # Fallback: parse platform name like "gpu-h200-sxm" → "H200".
    parts: list[str] = platform_name.split("-")
    for part in parts:
        if part.lower() == "gpu":
            continue
        # First segment after "gpu-" is the chip family.
        return part.upper()
    return platform_name.upper()


def _parse_resources(
    *,
    resources: dict[str, Any],
) -> tuple[int, int, int, int]:
    """Return (gpu_count, gpu_memory_gib, vcpu_count, ram_gib) from a preset."""
    gpu_count: int = int(resources.get("gpus", resources.get("gpu_count", 0)))
    gpu_mem: int = int(resources.get("gpu_memory_gibibytes", 0))
    vcpu_count: int = int(resources.get("vcpu_count", 0))
    ram: int = int(
        resources.get(
            "ram_gibibytes",
            resources.get("memory_gibibytes", 0),
        )
    )
    return gpu_count, gpu_mem, vcpu_count, ram


def _strip_cidr(*, address: str) -> str:
    if "/" in address:
        return address.split("/", 1)[0]
    return address


def _extract_ip(*, value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        addr: Any = value.get("address")
        if isinstance(addr, str) and len(addr) > 0:
            return _strip_cidr(address=addr)
    if isinstance(value, str) and len(value) > 0:
        return _strip_cidr(address=value)
    return None


def _extract_public_ip(*, interface: dict[str, Any]) -> str | None:
    return _extract_ip(value=interface.get("public_ip_address"))


def _extract_private_ip(*, interface: dict[str, Any]) -> str | None:
    private: Any = interface.get("private_ip_address")
    if private is not None:
        return _extract_ip(value=private)
    return _extract_ip(value=interface.get("ip_address"))


def _extract_boot_disk_id(*, instance: dict[str, Any]) -> str:
    status: dict[str, Any] = instance.get("status", {}) or {}
    disk_attachments: Any = status.get("disk_attachments")
    if isinstance(disk_attachments, list) and len(disk_attachments) > 0:
        first: Any = disk_attachments[0]
        if isinstance(first, dict):
            disk_id: Any = first.get("id")
            if isinstance(disk_id, str) and len(disk_id) > 0:
                return disk_id
    spec: dict[str, Any] = instance.get("spec", {}) or {}
    boot_disk: Any = spec.get("boot_disk")
    if isinstance(boot_disk, dict):
        existing: Any = boot_disk.get("existing_disk")
        if isinstance(existing, dict):
            disk_id_value: Any = existing.get("id")
            if isinstance(disk_id_value, str) and len(disk_id_value) > 0:
                return disk_id_value
    return ""


# ---------------------------------------------------------------------------
# CLI wrappers
# ---------------------------------------------------------------------------


def init_sdk(*, profile_name: str) -> None:
    """Verify the Nebius CLI is authenticated for ``profile_name``.

    Runs ``nebius -p <profile> iam whoami`` and raises
    ``NebiusProvisioningError`` on non-zero exit.
    """
    cmd: list[str] = ["nebius", "-p", profile_name, "iam", "whoami"]
    result: subprocess.CompletedProcess[str] = _run_cli(cmd=cmd)
    if result.returncode != 0:
        raise NebiusProvisioningError(
            f"nebius whoami failed (profile={profile_name}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def list_platforms(*, profile_name: str) -> list[NebiusOffer]:
    """Enumerate available compute platforms as ``NebiusOffer`` rows."""
    cmd: list[str] = [
        "nebius",
        "-p",
        profile_name,
        "compute",
        "platform",
        "list",
        "--format",
        "json",
    ]
    result: subprocess.CompletedProcess[str] = _run_cli(cmd=cmd)
    if result.returncode != 0:
        raise NebiusProvisioningError(
            f"nebius platform list failed: {result.stderr.strip() or result.stdout.strip()}"
        )

    payload: Any = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise NebiusProvisioningError(
            f"nebius platform list returned non-object payload: {payload!r}"
        )

    items: Any = payload.get("items", [])
    if not isinstance(items, list):
        raise NebiusProvisioningError(f"nebius platform list 'items' is not a list: {items!r}")

    offers: list[NebiusOffer] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata: dict[str, Any] = item.get("metadata", {}) or {}
        spec: dict[str, Any] = item.get("spec", {}) or {}
        platform_name: str = str(metadata.get("name", ""))
        quota_type: str | None = spec.get("gpu_count_quota_type")
        gpu_class: str = _derive_gpu_class(
            quota_type=quota_type if isinstance(quota_type, str) else None,
            platform_name=platform_name,
        )
        presets: Any = spec.get("presets", [])
        if not isinstance(presets, list):
            continue
        for preset in presets:
            if not isinstance(preset, dict):
                continue
            preset_name: str = str(preset.get("name", ""))
            resources: dict[str, Any] = preset.get("resources", {}) or {}
            gpu_count, gpu_mem, vcpu_count, ram = _parse_resources(
                resources=resources,
            )
            offers.append(
                NebiusOffer(
                    platform=platform_name,
                    preset=preset_name,
                    region="",
                    gpu_class=gpu_class,
                    gpu_count=gpu_count,
                    gpu_memory_gibibytes=gpu_mem,
                    vcpu_count=vcpu_count,
                    ram_gibibytes=ram,
                    hourly_cost_estimate_usd=None,
                ),
            )
    return offers


# ---------------------------------------------------------------------------
# SSH smoke gate
# ---------------------------------------------------------------------------


def _wait_for_ssh_reachable(
    *,
    host: str,
    port: int = 22,
    timeout_seconds: float = float(DEFAULT_SSH_SMOKE_GATE_TIMEOUT_SECONDS),
    poll_interval_seconds: float = DEFAULT_SSH_POLL_INTERVAL_SECONDS,
    per_attempt_timeout_seconds: float = DEFAULT_SSH_TCP_TIMEOUT_SECONDS,
) -> bool:
    """Return True once TCP/``port`` accepts connections to ``host``.

    Polls every ``poll_interval_seconds`` until the deadline. Returns False
    when the deadline elapses without a successful connection.
    """
    deadline: float = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (host, port),
                timeout=per_attempt_timeout_seconds,
            ):
                return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_seconds)
    return False


# ---------------------------------------------------------------------------
# Instance create / get / delete
# ---------------------------------------------------------------------------


def _build_cloud_init(
    *,
    ssh_public_key_path: Path,
    watchdog_config: WatchdogConfig | None = None,
    profile_name: str | None = None,
) -> str:
    key_contents: str = ssh_public_key_path.read_text(encoding="utf-8").strip()
    if watchdog_config is None:
        return f"#cloud-config\nssh_authorized_keys:\n  - {key_contents}\n"
    assert profile_name is not None, "profile_name is required when installing the watchdog"
    return render_nebius_cloud_init(
        ssh_public_key=key_contents,
        profile_name=profile_name,
        config=watchdog_config,
    )


def _create_instance(
    *,
    profile_name: str,
    project_id: str,
    name: str,
    platform: str,
    preset: str,
    boot_disk_size_gibibytes: int,
    image_id: str,
    subnet_id: str,
    cloud_init: str,
    service_account_id: str | None = None,
) -> dict[str, Any]:
    network_interfaces: str = json.dumps(
        [
            {
                "name": "eth0",
                "subnet_id": subnet_id,
                "ip_address": {},
                "public_ip_address": {},
            }
        ]
    )
    cmd: list[str] = [
        "nebius",
        "-p",
        profile_name,
        "compute",
        "instance",
        "create",
        "--parent-id",
        project_id,
        "--name",
        name,
        "--resources-platform",
        platform,
        "--resources-preset",
        preset,
        "--boot-disk-managed-disk-name",
        f"{name}-boot",
        "--boot-disk-managed-disk-type",
        "network_ssd",
        "--boot-disk-managed-disk-size-gibibytes",
        str(boot_disk_size_gibibytes),
        "--boot-disk-managed-disk-source-image-id",
        image_id,
        "--boot-disk-attach-mode",
        "read_write",
        "--network-interfaces",
        network_interfaces,
        "--cloud-init-user-data",
        cloud_init,
        "--format",
        "json",
    ]
    if service_account_id is not None:
        # Attaches the scoped SA (role limited to compute.instances.stop) so the
        # on-VM watchdog can stop this instance via a metadata token, no secret.
        cmd.extend(["--service-account-id", service_account_id])
    result: subprocess.CompletedProcess[str] = _run_cli(
        cmd=cmd,
        timeout_seconds=DEFAULT_CLI_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise NebiusProvisioningError(
            f"nebius instance create failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload: Any = json.loads(result.stdout) if len(result.stdout) > 0 else {}
    except json.JSONDecodeError as exc:
        raise NebiusProvisioningError(
            f"nebius instance create returned non-JSON output: {result.stdout!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise NebiusProvisioningError(
            f"nebius instance create returned non-object payload: {payload!r}"
        )
    return payload


def _get_instance(
    *,
    profile_name: str,
    instance_id: str,
) -> dict[str, Any]:
    cmd: list[str] = [
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
    ]
    result: subprocess.CompletedProcess[str] = _run_cli(cmd=cmd)
    if result.returncode != 0:
        raise NebiusProvisioningError(
            f"nebius instance get failed for {instance_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    payload: Any = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise NebiusProvisioningError(
            f"nebius instance get returned non-object payload: {payload!r}"
        )
    return payload


def _wait_for_running(
    *,
    profile_name: str,
    instance_id: str,
    max_wait_seconds: int,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    deadline: float = time.monotonic() + float(max_wait_seconds)
    last_state: str = ""
    while time.monotonic() < deadline:
        instance: dict[str, Any] = _get_instance(
            profile_name=profile_name,
            instance_id=instance_id,
        )
        status: dict[str, Any] = instance.get("status", {}) or {}
        state: str = str(status.get("state", ""))
        last_state = state
        if state == "RUNNING":
            return instance
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_seconds)
    raise NebiusProvisioningError(
        f"instance {instance_id} did not reach RUNNING within "
        f"{max_wait_seconds}s (last state: {last_state!r})"
    )


def _delete_instance(
    *,
    profile_name: str,
    instance_id: str,
) -> subprocess.CompletedProcess[str]:
    cmd: list[str] = [
        "nebius",
        "-p",
        profile_name,
        "compute",
        "instance",
        "delete",
        "--id",
        instance_id,
    ]
    return _run_cli(cmd=cmd)


# ---------------------------------------------------------------------------
# acquire / teardown
# ---------------------------------------------------------------------------


def acquire(
    *,
    task_id: str,
    gpu_class: str,
    preset: str,
    image_id: str,
    ssh_public_key_path: Path,
    profile_name: str,
    region: str,
    tenant_id: str,
    project_id: str,
    subnet_id: str,
    boot_disk_size_gibibytes: int = DEFAULT_BOOT_DISK_SIZE_GIBIBYTES,
    name: str | None = None,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
    ssh_smoke_gate_timeout_seconds: int = DEFAULT_SSH_SMOKE_GATE_TIMEOUT_SECONDS,
    lock_dir: Path | None = None,
    watchdog_config: WatchdogConfig | None = None,
    watchdog_service_account_id: str | None = None,
) -> ProvisionResult:
    """Acquire a single Nebius instance for ``task_id``.

    Raises ``NebiusProvisioningError`` on any failure. The lock file is held
    for the lifetime of the resulting instance; ``teardown`` clears it.

    When ``watchdog_config`` and ``watchdog_service_account_id`` are both
    provided, the instance is provisioned with the idle dead-man's-switch
    watchdog (cloud-init systemd service) and the scoped service account that
    lets it stop itself. See ``watchdog_provisioning.render_nebius_cloud_init``.
    """
    started_monotonic: float = time.monotonic()
    resolved_lock_dir: Path = _resolve_lock_dir(lock_dir=lock_dir)
    _acquire_lock(lock_dir=resolved_lock_dir, task_id=task_id)

    instance_name: str = name if name is not None else f"arf-{task_id}"
    failed_attempts: list[FailedAttempt] = []
    instance_id: str | None = None
    cloud_init: str = _build_cloud_init(
        ssh_public_key_path=ssh_public_key_path,
        watchdog_config=watchdog_config,
        profile_name=profile_name if watchdog_config is not None else None,
    )

    platform_name: str = _platform_name_for_gpu_class(gpu_class=gpu_class)
    offer: NebiusOffer = NebiusOffer(
        platform=platform_name,
        preset=preset,
        region=region,
        gpu_class=gpu_class,
        gpu_count=0,
        gpu_memory_gibibytes=0,
        vcpu_count=0,
        ram_gibibytes=0,
        hourly_cost_estimate_usd=None,
    )

    try:
        create_payload: dict[str, Any] = _create_instance(
            profile_name=profile_name,
            project_id=project_id,
            name=instance_name,
            platform=platform_name,
            preset=preset,
            boot_disk_size_gibibytes=boot_disk_size_gibibytes,
            image_id=image_id,
            subnet_id=subnet_id,
            cloud_init=cloud_init,
            service_account_id=watchdog_service_account_id,
        )
        metadata: dict[str, Any] = create_payload.get("metadata", {}) or {}
        instance_id_value: Any = metadata.get("id")
        if not isinstance(instance_id_value, str) or len(instance_id_value) == 0:
            raise NebiusProvisioningError(
                f"nebius instance create returned no metadata.id: {create_payload!r}"
            )
        instance_id = instance_id_value

        running: dict[str, Any] = _wait_for_running(
            profile_name=profile_name,
            instance_id=instance_id,
            max_wait_seconds=max_wait_seconds,
        )

        status: dict[str, Any] = running.get("status", {}) or {}
        interfaces: Any = status.get("network_interfaces", [])
        public_ip: str | None = None
        private_ip: str = ""
        if isinstance(interfaces, list) and len(interfaces) > 0:
            first_interface: Any = interfaces[0]
            if isinstance(first_interface, dict):
                public_ip = _extract_public_ip(interface=first_interface)
                private_candidate: str | None = _extract_private_ip(
                    interface=first_interface,
                )
                if private_candidate is not None:
                    private_ip = private_candidate

        if public_ip is None:
            raise NebiusProvisioningError(
                f"instance {instance_id} has no public IP after reaching RUNNING"
            )

        reachable: bool = _wait_for_ssh_reachable(
            host=public_ip,
            port=22,
            timeout_seconds=float(ssh_smoke_gate_timeout_seconds),
        )
        if not reachable:
            raise NebiusProvisioningError(
                f"SSH smoke gate failed: TCP/22 on {public_ip} not reachable within "
                f"{ssh_smoke_gate_timeout_seconds}s"
            )

        boot_disk_id: str = _extract_boot_disk_id(instance=running)

        acquire_result: AcquireResult = AcquireResult(
            instance_id=instance_id,
            public_ip=public_ip,
            private_ip=private_ip,
            boot_disk_id=boot_disk_id,
            region=region,
            platform=platform_name,
            preset=preset,
            image_id=image_id,
            acquired_at=_now_iso(),
            hourly_cost_estimate_usd=None,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        total_seconds: float = time.monotonic() - started_monotonic
        return ProvisionResult(
            acquire_result=acquire_result,
            failed_attempts=failed_attempts,
            total_provisioning_seconds=total_seconds,
        )
    except NebiusProvisioningError as exc:
        failed_attempts.append(
            FailedAttempt(
                offer=offer,
                failure_reason=str(exc),
                failed_at=_now_iso(),
                wasted_cost_usd=0.0,
            ),
        )
        if instance_id is not None:
            try:
                teardown(
                    task_id=task_id,
                    instance_id=instance_id,
                    profile_name=profile_name,
                    lock_dir=resolved_lock_dir,
                )
            except Exception:  # noqa: BLE001 - best-effort cleanup
                _release_lock(lock_dir=resolved_lock_dir, task_id=task_id)
        else:
            _release_lock(lock_dir=resolved_lock_dir, task_id=task_id)
        raise


def _platform_name_for_gpu_class(*, gpu_class: str) -> str:
    """Return the canonical Nebius platform identifier for a GPU class."""
    chip: str = gpu_class.lower()
    return f"gpu-{chip}-sxm"


def teardown(
    *,
    task_id: str,
    instance_id: str,
    profile_name: str,
    lock_dir: Path | None = None,
) -> TeardownResult:
    """Delete a Nebius instance and clear the corresponding lock file.

    Treats "not found" as idempotent success. The lock file is always
    cleared, regardless of CLI exit code.
    """
    resolved_lock_dir: Path = _resolve_lock_dir(lock_dir=lock_dir)
    started_monotonic: float = time.monotonic()
    error_message: str | None = None
    success: bool = True

    try:
        result: subprocess.CompletedProcess[str] = _delete_instance(
            profile_name=profile_name,
            instance_id=instance_id,
        )
        if result.returncode != 0:
            stderr_lower: str = result.stderr.lower()
            if (
                "notfound" in stderr_lower
                or "not found" in stderr_lower
                or "does not exist" in stderr_lower
            ):
                success = True
            else:
                success = False
                error_message = result.stderr.strip() or result.stdout.strip() or "delete failed"
    except subprocess.SubprocessError as exc:
        success = False
        error_message = str(exc)

    _release_lock(lock_dir=resolved_lock_dir, task_id=task_id)
    duration: float = time.monotonic() - started_monotonic

    return TeardownResult(
        instance_id=instance_id,
        terminated_at=_now_iso(),
        success=success,
        error_message=error_message,
        teardown_duration_seconds=duration,
    )


# ---------------------------------------------------------------------------
# Machine-log entry construction
# ---------------------------------------------------------------------------


def _offer_to_dict(*, offer: NebiusOffer) -> dict[str, Any]:
    return dataclasses.asdict(offer)


def _failed_attempt_to_dict(*, attempt: FailedAttempt) -> dict[str, Any]:
    return {
        "offer": _offer_to_dict(offer=attempt.offer),
        "failure_reason": attempt.failure_reason,
        "failed_at": attempt.failed_at,
        "wasted_cost_usd": attempt.wasted_cost_usd,
    }


def to_machine_log_entry(
    *,
    acquire_result: AcquireResult,
    task_id: str,
    gpu_class: str,
    failed_attempts: list[FailedAttempt],
    total_provisioning_seconds: float,
) -> dict[str, object]:
    """Build a ``machine_log.json`` entry (spec v5) for a Nebius acquire.

    The output conforms to the remote-machines spec v5 ``provider: "nebius"``
    branch (see ``arf/specifications/remote_machines_specification.md``).
    """
    serialised: list[dict[str, Any]] = [
        _failed_attempt_to_dict(attempt=att) for att in failed_attempts
    ]
    return {
        "spec_version": SPEC_VERSION,
        "provider": PROVIDER_SLUG,
        "task_id": task_id,
        "gpu_class": gpu_class,
        "instance_id": acquire_result.instance_id,
        "platform": acquire_result.platform,
        "preset": acquire_result.preset,
        "image_id": acquire_result.image_id,
        "region": acquire_result.region,
        "tenant_id": acquire_result.tenant_id,
        "project_id": acquire_result.project_id,
        "public_ip": acquire_result.public_ip,
        "private_ip": acquire_result.private_ip,
        "boot_disk_id": acquire_result.boot_disk_id,
        "acquired_at": acquire_result.acquired_at,
        "hourly_cost_estimate_usd": acquire_result.hourly_cost_estimate_usd,
        "total_provisioning_seconds": total_provisioning_seconds,
        "failed_attempts": serialised,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="nebius_machines",
        description="Nebius GPU provisioning CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    whoami_parser: argparse.ArgumentParser = subparsers.add_parser(
        "whoami",
        help="Verify the Nebius CLI is authenticated.",
    )
    whoami_parser.add_argument("--profile", type=str, default="compute")

    list_parser: argparse.ArgumentParser = subparsers.add_parser(
        "list",
        help="List Nebius compute platforms and presets.",
    )
    list_parser.add_argument("--profile", type=str, default="compute")

    acquire_parser: argparse.ArgumentParser = subparsers.add_parser(
        "acquire",
        help="Acquire a Nebius instance for a task.",
    )
    acquire_parser.add_argument("task_id", type=str)
    acquire_parser.add_argument("--gpu-class", type=str, required=True)
    acquire_parser.add_argument("--preset", type=str, required=True)
    acquire_parser.add_argument("--image-id", type=str, required=True)
    acquire_parser.add_argument(
        "--ssh-public-key-path",
        type=str,
        required=True,
        dest="ssh_public_key_path",
    )
    acquire_parser.add_argument("--tenant-id", type=str, required=True)
    acquire_parser.add_argument("--project-id", type=str, required=True)
    acquire_parser.add_argument("--subnet-id", type=str, required=True)
    acquire_parser.add_argument("--profile", type=str, default="compute")
    acquire_parser.add_argument("--region", type=str, default="eu-north1")
    acquire_parser.add_argument(
        "--boot-disk-size-gibibytes",
        type=int,
        default=DEFAULT_BOOT_DISK_SIZE_GIBIBYTES,
    )
    acquire_parser.add_argument(
        "--max-wait-seconds",
        type=int,
        default=DEFAULT_MAX_WAIT_SECONDS,
    )
    acquire_parser.add_argument(
        "--smoke-gate-timeout-seconds",
        type=int,
        default=DEFAULT_SSH_SMOKE_GATE_TIMEOUT_SECONDS,
    )
    acquire_parser.add_argument("--name", type=str, default=None)

    teardown_parser: argparse.ArgumentParser = subparsers.add_parser(
        "teardown",
        help="Delete a Nebius instance and clear its lock file.",
    )
    teardown_parser.add_argument("task_id", type=str)
    teardown_parser.add_argument("--instance-id", type=str, required=True)
    teardown_parser.add_argument("--profile", type=str, default="compute")

    return parser


def _run_whoami_cmd(*, args: argparse.Namespace) -> int:
    init_sdk(profile_name=args.profile)
    print("ok")
    return 0


def _run_list_cmd(*, args: argparse.Namespace) -> int:
    offers: list[NebiusOffer] = list_platforms(profile_name=args.profile)
    payload: list[dict[str, Any]] = [dataclasses.asdict(offer) for offer in offers]
    print(json.dumps(payload, indent=2))
    return 0


def _run_acquire_cmd(*, args: argparse.Namespace) -> int:
    init_sdk(profile_name=args.profile)
    result: ProvisionResult = acquire(
        task_id=args.task_id,
        gpu_class=args.gpu_class,
        preset=args.preset,
        image_id=args.image_id,
        ssh_public_key_path=Path(args.ssh_public_key_path),
        profile_name=args.profile,
        region=args.region,
        tenant_id=args.tenant_id,
        project_id=args.project_id,
        subnet_id=args.subnet_id,
        boot_disk_size_gibibytes=args.boot_disk_size_gibibytes,
        name=args.name,
        max_wait_seconds=args.max_wait_seconds,
        ssh_smoke_gate_timeout_seconds=args.smoke_gate_timeout_seconds,
    )
    payload: dict[str, Any] = {
        "acquire_result": dataclasses.asdict(result.acquire_result),
        "failed_attempts": [_failed_attempt_to_dict(attempt=att) for att in result.failed_attempts],
        "total_provisioning_seconds": result.total_provisioning_seconds,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _run_teardown_cmd(*, args: argparse.Namespace) -> int:
    init_sdk(profile_name=args.profile)
    result: TeardownResult = teardown(
        task_id=args.task_id,
        instance_id=args.instance_id,
        profile_name=args.profile,
    )
    print(json.dumps(dataclasses.asdict(result), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = _build_argparser()
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        if args.command == "whoami":
            return _run_whoami_cmd(args=args)
        if args.command == "list":
            return _run_list_cmd(args=args)
        if args.command == "acquire":
            return _run_acquire_cmd(args=args)
        if args.command == "teardown":
            return _run_teardown_cmd(args=args)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except NebiusProvisioningError as exc:
        print(f"nebius provisioning error: {exc}", file=sys.stderr)
        return 75
    except Exception as exc:  # noqa: BLE001 - CLI must map all errors
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
