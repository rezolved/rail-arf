"""Vast.ai GPU provisioning utilities."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from arf.scripts.utils.watchdog_provisioning import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_IDLE_THRESHOLD_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    WatchdogConfig,
    render_vast_onstart,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_FILTERS: str = "rentable=true verified=true compute_cap<1200 cuda_max_good>=12.6"

GPU_SPEED_TIERS: dict[str, float] = {
    "GTX 1080 Ti": 0.35,
    "RTX 2080 Ti": 0.55,
    "RTX 3060": 0.40,
    "RTX 3070": 0.60,
    "RTX 3080": 0.80,
    "RTX 3090": 1.0,
    "RTX 4070": 0.90,
    "RTX 4070 Ti": 1.05,
    "RTX 4080": 1.30,
    "RTX 4090": 1.60,
    "RTX 5060 Ti": 1.00,
    "RTX 5070 Ti": 1.50,
    "RTX 5090": 2.50,
    "A100 40GB": 1.80,
    "A100 80GB": 2.00,
    "H100": 3.00,
    "H200": 3.50,
    "RTX PRO 6000 S": 2.80,
    "RTX PRO 6000 WS": 2.80,
}

RELIABILITY_THRESHOLDS: list[tuple[float, float]] = [
    (1.0, 0.95),
    (5.0, 0.98),
    (24.0, 0.995),
    (float("inf"), 0.999),
]

MAX_RETRY_OFFERS: int = 3
POLL_INTERVAL_SECONDS: float = 30.0
CREATION_TIMEOUT_SECONDS: float = 600.0

# SSH-ready handshake: when an instance reaches actual_status="running", the
# host's SSH daemon may not yet have propagated the team's pubkey. Poll for
# successful nvidia-smi rather than failing on the first refused connection.
SSH_READY_POLL_INTERVAL_SECONDS: int = 15
SSH_READY_TIMEOUT_SECONDS: int = 300
SSH_READY_PER_ATTEMPT_TIMEOUT_SECONDS: int = 30

SIMILAR_SPEED_TOLERANCE: float = 0.20
DEFAULT_SPEED_TIER: float = 1.0

SPEC_VERSION: str = "4"
PROVIDER_SLUG: str = "vast_ai"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VastProvisioningError(RuntimeError):
    """Raised when Vast.ai provisioning exhausts retries or finds no offers."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    gpu_name: str | None
    num_gpus: int
    min_gpu_ram: float | None
    min_cpu_ram: float | None
    min_disk: float | None
    min_reliability: float
    estimated_hours_reference: float
    extra_filters: str | None = None


@dataclass(frozen=True, slots=True)
class VastOffer:
    offer_id: int
    gpu: str
    gpu_count: int
    gpu_ram_gb: float
    cpu_ram_gb: float
    disk_gb: float
    price_per_hour: float
    reliability: float
    location: str
    compute_cap: int
    cuda_max_good: float


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    offer_id: int
    instance_id: str | None
    gpu: str
    failure_reason: str
    failure_phase: str
    duration_seconds: float
    wasted_cost_usd: float
    timestamp: str


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    instance_id: str
    offer: VastOffer
    ssh_host: str
    ssh_port: int
    gpu_verified: str
    cuda_version: str
    label: str
    created_at: str
    ready_at: str
    search_started_at: str
    total_provisioning_seconds: float
    failed_attempts: list[FailedAttempt]
    image: str = ""
    disk_gb: int = 0
    selection_rationale: str = ""
    search_criteria: SearchCriteria | None = None


@dataclass(frozen=True, slots=True)
class DestroyResult:
    instance_id: str
    destroyed_at: str
    total_duration_hours: float
    total_cost_usd: float


# ---------------------------------------------------------------------------
# Pure helpers (query, ranking, thresholds)
# ---------------------------------------------------------------------------


def build_query_string(*, criteria: SearchCriteria) -> str:
    parts: list[str] = [DEFAULT_FILTERS]

    if criteria.gpu_name is not None:
        parts.append(f"gpu_name={criteria.gpu_name}")

    if criteria.num_gpus > 1:
        parts.append(f"num_gpus={criteria.num_gpus}")

    if criteria.min_gpu_ram is not None:
        parts.append(f"gpu_ram>={criteria.min_gpu_ram}")

    if criteria.min_cpu_ram is not None:
        parts.append(f"cpu_ram>={criteria.min_cpu_ram}")

    if criteria.min_disk is not None:
        parts.append(f"disk_space>={criteria.min_disk}")

    if criteria.min_reliability > 0.0:
        parts.append(f"reliability>={criteria.min_reliability}")

    if criteria.extra_filters is not None:
        parts.append(criteria.extra_filters)

    return " ".join(parts)


def _estimate_hours(
    *,
    offer: VastOffer,
    estimated_hours_reference: float,
) -> float:
    reference_speed: float = GPU_SPEED_TIERS.get("RTX 3090", DEFAULT_SPEED_TIER)
    offer_speed: float = GPU_SPEED_TIERS.get(offer.gpu, DEFAULT_SPEED_TIER)
    assert offer_speed > 0.0, "GPU speed tier is positive"
    return estimated_hours_reference * (reference_speed / offer_speed)


def rank_offers(
    *,
    offers: list[VastOffer],
    estimated_hours_reference: float,
) -> list[VastOffer]:
    decorated: list[tuple[float, float, VastOffer]] = []
    for offer in offers:
        est_hours: float = _estimate_hours(
            offer=offer,
            estimated_hours_reference=estimated_hours_reference,
        )
        decorated.append((est_hours, offer.price_per_hour, offer))

    decorated.sort(key=lambda t: (t[0], t[1]))

    result: list[VastOffer] = []
    for est_hours, price, offer in decorated:
        inserted: bool = False
        for i, existing in enumerate(result):
            existing_hours: float = _estimate_hours(
                offer=existing,
                estimated_hours_reference=estimated_hours_reference,
            )
            ratio: float = abs(est_hours - existing_hours) / max(existing_hours, 1e-9)
            if ratio <= SIMILAR_SPEED_TOLERANCE:
                if price < existing.price_per_hour:
                    result.insert(i, offer)
                    inserted = True
                    break
            elif est_hours < existing_hours:
                result.insert(i, offer)
                inserted = True
                break
        if not inserted:
            result.append(offer)

    return result


def reliability_threshold_for(*, estimated_hours: float) -> float:
    for max_hours, threshold in RELIABILITY_THRESHOLDS:
        if estimated_hours <= max_hours:
            return threshold
    return RELIABILITY_THRESHOLDS[-1][1]


# ---------------------------------------------------------------------------
# SDK seams (monkeypatched in tests)
# ---------------------------------------------------------------------------


def _sdk_factory(*, api_key: str) -> Any:
    """Construct a live VastAI SDK handle. Monkeypatched in tests."""
    import vastai_sdk  # type: ignore[import-untyped]

    return vastai_sdk.VastAI(api_key=api_key)


def _default_ssh_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Default ssh executor: subprocess.run with a per-attempt timeout."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        timeout=float(SSH_READY_PER_ATTEMPT_TIMEOUT_SECONDS),
    )


def _parse_nvidia_smi_output(*, stdout: str) -> dict[str, str]:
    first_line: str = stdout.strip().splitlines()[0]
    parts: list[str] = [p.strip() for p in first_line.split(",")]
    gpu_name: str = parts[0] if len(parts) > 0 else ""
    driver_version: str = parts[1] if len(parts) > 1 else ""
    return {"gpu_verified": gpu_name, "cuda_version": driver_version}


def _verify_gpu_via_ssh(
    *,
    sdk: Any,
    instance_id: str,
    ssh_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    sleep_func: Callable[[float], None] | None = None,
    monotonic_func: Callable[[], float] | None = None,
) -> dict[str, str]:
    """Verify GPU availability over SSH with a polling retry loop.

    Vast.ai's SSH daemon may need extra time after ``actual_status=running`` to
    propagate the team pubkey, so a single-shot probe is unreliable. This
    function polls ``ssh ... nvidia-smi`` every
    ``SSH_READY_POLL_INTERVAL_SECONDS`` until either the command succeeds (and
    yields parseable output) or the ``SSH_READY_TIMEOUT_SECONDS`` budget is
    exhausted.

    The ``ssh_runner``, ``sleep_func``, and ``monotonic_func`` seams exist so
    tests can drive the loop without real network calls or sleeps.
    """
    resolved_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = (
        _default_ssh_runner if ssh_runner is None else ssh_runner
    )
    resolved_sleep: Callable[[float], None] = time.sleep if sleep_func is None else sleep_func
    resolved_monotonic: Callable[[], float] = (
        time.monotonic if monotonic_func is None else monotonic_func
    )

    info: dict[str, Any] = sdk.show_instance(id=instance_id)
    ssh_host: str = str(info.get("ssh_host", ""))
    ssh_port: int = int(info.get("ssh_port", 22))

    cmd: list[str] = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ConnectTimeout={SSH_READY_PER_ATTEMPT_TIMEOUT_SECONDS}",
        "-p",
        str(ssh_port),
        f"root@{ssh_host}",
        "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader",
    ]

    start: float = resolved_monotonic()
    deadline: float = start + float(SSH_READY_TIMEOUT_SECONDS)
    attempt_count: int = 0
    last_error: BaseException | None = None

    while resolved_monotonic() < deadline:
        attempt_count += 1
        try:
            completed: subprocess.CompletedProcess[str] = resolved_runner(cmd)
            if completed.returncode == 0 and len(completed.stdout.strip()) > 0:
                return _parse_nvidia_smi_output(stdout=completed.stdout)
            last_error = RuntimeError(
                f"nvidia-smi returned code {completed.returncode}: "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )
        except subprocess.CalledProcessError as exc:
            last_error = exc
        except subprocess.TimeoutExpired as exc:
            last_error = exc

        if resolved_monotonic() >= deadline:
            break
        resolved_sleep(float(SSH_READY_POLL_INTERVAL_SECONDS))

    elapsed: float = resolved_monotonic() - start
    last_error_detail: str = _describe_ssh_error(error=last_error)
    raise RuntimeError(
        "SSH-ready handshake exhausted after "
        f"{attempt_count} attempts over {elapsed:.1f}s "
        f"(budget {SSH_READY_TIMEOUT_SECONDS}s); "
        f"last error: {last_error_detail}"
    )


def _describe_ssh_error(*, error: BaseException | None) -> str:
    if error is None:
        return "no attempts recorded"
    if isinstance(error, subprocess.CalledProcessError):
        stderr: str = (error.stderr or "").strip()
        stdout: str = (error.stdout or "").strip()
        return (
            f"CalledProcessError(returncode={error.returncode}, "
            f"stdout={stdout!r}, stderr={stderr!r})"
        )
    if isinstance(error, subprocess.TimeoutExpired):
        return f"TimeoutExpired(timeout={error.timeout!r})"
    return repr(error)


# ---------------------------------------------------------------------------
# SDK init
# ---------------------------------------------------------------------------


def init_sdk(*, api_key: str | None = None) -> Any:
    """Return a VastAI SDK handle constructed via ``_sdk_factory``.

    Resolution order: explicit ``api_key`` argument, then ``VAST_API_KEY`` env var.
    Raises ``RuntimeError`` when neither source is available.
    """
    resolved_key: str | None = api_key
    if resolved_key is None:
        env_key: str | None = os.environ.get("VAST_API_KEY")
        if env_key is not None and len(env_key) > 0:
            resolved_key = env_key

    if resolved_key is None:
        raise RuntimeError("Vast.ai API key not provided and VAST_API_KEY env var is not set.")

    return _sdk_factory(api_key=resolved_key)


# ---------------------------------------------------------------------------
# Offer parsing and search
# ---------------------------------------------------------------------------


def _parse_offer_row(*, row: dict[str, Any]) -> VastOffer:
    """Convert a raw Vast.ai search-offers row into a typed ``VastOffer``."""
    gpu_ram_mb: float = float(row.get("gpu_ram", 0))
    cpu_ram_mb: float = float(row.get("cpu_ram", 0))
    return VastOffer(
        offer_id=int(row["id"]),
        gpu=str(row.get("gpu_name", "")),
        gpu_count=int(row.get("num_gpus", 1)),
        gpu_ram_gb=gpu_ram_mb / 1024.0,
        cpu_ram_gb=cpu_ram_mb / 1024.0,
        disk_gb=float(row.get("disk_space", 0.0)),
        price_per_hour=float(row.get("dph_total", 0.0)),
        reliability=float(row.get("reliability2", 0.0)),
        location=str(row.get("geolocation", "")),
        compute_cap=int(row.get("compute_cap", 0)),
        cuda_max_good=float(row.get("cuda_max_good", 0.0)),
    )


def search_offers(*, sdk: Any, criteria: SearchCriteria) -> list[VastOffer]:
    """Search Vast.ai for offers matching ``criteria`` and return ranked list."""
    query: str = build_query_string(criteria=criteria)
    rows: list[dict[str, Any]] = list(sdk.search_offers(query=query))
    offers: list[VastOffer] = [_parse_offer_row(row=row) for row in rows]
    return rank_offers(
        offers=offers,
        estimated_hours_reference=criteria.estimated_hours_reference,
    )


# ---------------------------------------------------------------------------
# Acquire / destroy
# ---------------------------------------------------------------------------


def label_instance(*, sdk: Any, instance_id: str, label: str) -> None:
    sdk.label_instance(
        id=instance_id,
        label=label,
    )


def _classify_failure_phase(*, reason: str) -> str:
    lowered: str = reason.lower()
    if "gpu" in lowered or "nvidia" in lowered or "compute capability" in lowered:
        return "gpu_verification"
    if "ssh" in lowered:
        return "ssh"
    if "did not reach 'running'" in lowered or "did not reach running" in lowered:
        return "waiting"
    return "creation"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _wait_for_running(
    *,
    sdk: Any,
    instance_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    deadline: float = time.monotonic() + timeout_seconds
    last_info: dict[str, Any] = {}
    while time.monotonic() < deadline:
        info: dict[str, Any] = sdk.show_instance(id=instance_id)
        last_info = info
        status: str = str(info.get("actual_status", ""))
        if status == "running":
            return info
        time.sleep(poll_interval_seconds)
    raise RuntimeError(
        f"Instance {instance_id} did not reach 'running' within "
        f"{timeout_seconds:.0f}s (last status: {last_info.get('actual_status')!r})"
    )


def _format_selection_rationale(
    *,
    offer: VastOffer,
    estimated_hours: float,
) -> str:
    total_estimated: float = estimated_hours * offer.price_per_hour
    return (
        f"Selected {offer.gpu} x{offer.gpu_count} at ${offer.price_per_hour:.3f}/hr "
        f"(estimated {estimated_hours:.2f}h, ~${total_estimated:.2f} total), "
        f"reliability {offer.reliability:.3f}, location {offer.location}"
    )


def acquire(
    *,
    sdk: Any,
    task_id: str,
    criteria: SearchCriteria,
    image: str,
    disk_gb: int,
    label: str,
    onstart: str | None = None,
) -> ProvisionResult:
    """Acquire a Vast.ai instance for ``task_id`` honouring ``criteria``.

    Tries up to ``MAX_RETRY_OFFERS`` ranked offers. On every-offer failure or
    no offers found, raises ``VastProvisioningError``.

    When ``onstart`` is provided it is forwarded to the instance as its startup
    script (used to install the idle dead-man's-switch watchdog; see
    ``watchdog_provisioning.render_vast_onstart``).
    """
    search_started_at: str = _now_iso()
    offers: list[VastOffer] = search_offers(sdk=sdk, criteria=criteria)
    if len(offers) == 0:
        raise VastProvisioningError(
            f"No Vast.ai offers matched search criteria for task {task_id}."
        )

    failed_attempts: list[FailedAttempt] = []
    candidates: list[VastOffer] = offers[:MAX_RETRY_OFFERS]

    for offer in candidates:
        attempt_started: datetime = datetime.now(tz=UTC)
        attempt_started_iso: str = attempt_started.isoformat()
        instance_id: str | None = None
        try:
            create_kwargs: dict[str, Any] = {
                "id": offer.offer_id,
                "image": image,
                "disk": disk_gb,
                "label": label,
            }
            if onstart is not None:
                # SDK kwarg is `onstart_cmd` (maps to API field `onstart`); the
                # bare `onstart` kwarg is rejected by the SDK allow-list.
                create_kwargs["onstart_cmd"] = onstart
            create_response: Any = sdk.create_instance(**create_kwargs)
            if not isinstance(create_response, dict):
                raise RuntimeError(
                    f"Vast.ai create_instance returned non-dict: {create_response!r}"
                )
            new_contract: Any = create_response.get("new_contract")
            if new_contract is None:
                raise RuntimeError(
                    f"Vast.ai create_instance response missing 'new_contract': {create_response!r}"
                )
            instance_id = str(new_contract)
            created_at: str = _now_iso()

            with contextlib.suppress(Exception):
                label_instance(sdk=sdk, instance_id=instance_id, label=label)

            info: dict[str, Any] = _wait_for_running(
                sdk=sdk,
                instance_id=instance_id,
                timeout_seconds=CREATION_TIMEOUT_SECONDS,
                poll_interval_seconds=POLL_INTERVAL_SECONDS,
            )
            ssh_host: str = str(info.get("ssh_host", ""))
            ssh_port: int = int(info.get("ssh_port", 22))

            verification: dict[str, str] = _verify_gpu_via_ssh(sdk=sdk, instance_id=instance_id)

            ready_dt: datetime = datetime.now(tz=UTC)
            ready_at: str = ready_dt.isoformat()
            search_started_dt: datetime = datetime.fromisoformat(search_started_at)
            total_provisioning_seconds: float = (ready_dt - search_started_dt).total_seconds()

            estimated_hours: float = _estimate_hours(
                offer=offer,
                estimated_hours_reference=criteria.estimated_hours_reference,
            )
            rationale: str = _format_selection_rationale(
                offer=offer,
                estimated_hours=estimated_hours,
            )

            return ProvisionResult(
                instance_id=instance_id,
                offer=offer,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                gpu_verified=verification["gpu_verified"],
                cuda_version=verification["cuda_version"],
                label=label,
                created_at=created_at,
                ready_at=ready_at,
                search_started_at=search_started_at,
                total_provisioning_seconds=total_provisioning_seconds,
                failed_attempts=failed_attempts,
                image=image,
                disk_gb=disk_gb,
                selection_rationale=rationale,
                search_criteria=criteria,
            )
        except Exception as exc:  # noqa: BLE001 - retry across all failures
            attempt_ended: datetime = datetime.now(tz=UTC)
            duration_seconds: float = (attempt_ended - attempt_started).total_seconds()
            wasted_cost: float = (duration_seconds / 3600.0) * offer.price_per_hour
            reason: str = str(exc)
            phase: str = _classify_failure_phase(reason=reason)
            failed_attempts.append(
                FailedAttempt(
                    offer_id=offer.offer_id,
                    instance_id=instance_id,
                    gpu=offer.gpu,
                    failure_reason=reason,
                    failure_phase=phase,
                    duration_seconds=duration_seconds,
                    wasted_cost_usd=wasted_cost,
                    timestamp=attempt_started_iso,
                )
            )
            if instance_id is not None:
                with contextlib.suppress(Exception):
                    sdk.destroy_instance(id=instance_id)
            continue

    phases: list[str] = [att.failure_phase for att in failed_attempts]
    raise VastProvisioningError(
        f"All {len(failed_attempts)} Vast.ai offers failed for task {task_id}; "
        f"failure phases: {phases}"
    )


def destroy_and_confirm(
    *,
    sdk: Any,
    instance_id: str,
    created_at: str,
    price_per_hour: float,
) -> DestroyResult:
    sdk.destroy_instance(id=instance_id)

    now: datetime = datetime.now(tz=UTC)
    destroyed_at: str = now.isoformat()

    created_dt: datetime = datetime.fromisoformat(created_at)
    duration_seconds: float = (now - created_dt).total_seconds()
    duration_hours: float = duration_seconds / 3600.0
    total_cost: float = duration_hours * price_per_hour

    return DestroyResult(
        instance_id=instance_id,
        destroyed_at=destroyed_at,
        total_duration_hours=duration_hours,
        total_cost_usd=total_cost,
    )


# ---------------------------------------------------------------------------
# Machine-log entry construction
# ---------------------------------------------------------------------------


def _selected_offer_dict(*, offer: VastOffer) -> dict[str, Any]:
    return {
        "offer_id": offer.offer_id,
        "gpu": offer.gpu,
        "gpu_count": offer.gpu_count,
        "gpu_ram_gb": offer.gpu_ram_gb,
        "cpu_ram_gb": offer.cpu_ram_gb,
        "disk_gb": offer.disk_gb,
        "price_per_hour": offer.price_per_hour,
        "reliability": offer.reliability,
        "location": offer.location,
    }


def _search_criteria_dict(*, criteria: SearchCriteria | None) -> dict[str, Any]:
    if criteria is None:
        return {
            "gpu_name": None,
            "num_gpus": 1,
            "min_gpu_ram": None,
            "min_cpu_ram": None,
            "min_disk": None,
            "min_reliability": 0.0,
            "extra_filters": None,
        }
    return {
        "gpu_name": criteria.gpu_name,
        "num_gpus": criteria.num_gpus,
        "min_gpu_ram": criteria.min_gpu_ram,
        "min_cpu_ram": criteria.min_cpu_ram,
        "min_disk": criteria.min_disk,
        "min_reliability": criteria.min_reliability,
        "extra_filters": criteria.extra_filters,
    }


def to_machine_log_entry(
    *,
    provision_result: ProvisionResult,
    destroy_result: DestroyResult | None = None,
) -> dict[str, Any]:
    """Build a ``machine_log.json`` entry from provision and destroy outcomes."""
    failed_attempts_serialised: list[dict[str, Any]] = [
        dataclasses.asdict(att) for att in provision_result.failed_attempts
    ]

    destroyed_at: str | None
    total_duration_hours: float | None
    total_cost_usd: float | None
    if destroy_result is None:
        destroyed_at = None
        total_duration_hours = None
        total_cost_usd = None
    else:
        destroyed_at = destroy_result.destroyed_at
        total_duration_hours = destroy_result.total_duration_hours
        total_cost_usd = destroy_result.total_cost_usd

    return {
        "spec_version": SPEC_VERSION,
        "provider": PROVIDER_SLUG,
        "instance_id": provision_result.instance_id,
        "offer_id": provision_result.offer.offer_id,
        "search_criteria": _search_criteria_dict(
            criteria=provision_result.search_criteria,
        ),
        "selected_offer": _selected_offer_dict(offer=provision_result.offer),
        "selection_rationale": provision_result.selection_rationale,
        "image": provision_result.image,
        "disk_gb": provision_result.disk_gb,
        "label": provision_result.label,
        "ssh_host": provision_result.ssh_host,
        "ssh_port": provision_result.ssh_port,
        "gpu_verified": provision_result.gpu_verified,
        "cuda_version": provision_result.cuda_version,
        "created_at": provision_result.created_at,
        "ready_at": provision_result.ready_at,
        "destroyed_at": destroyed_at,
        "total_duration_hours": total_duration_hours,
        "total_cost_usd": total_cost_usd,
        "search_started_at": provision_result.search_started_at,
        "total_provisioning_seconds": provision_result.total_provisioning_seconds,
        "failed_attempts": failed_attempts_serialised,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="vast_machines",
        description="Vast.ai GPU provisioning CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    acquire_parser: argparse.ArgumentParser = subparsers.add_parser(
        "acquire",
        help="Acquire a Vast.ai instance for a task.",
    )
    acquire_parser.add_argument("task_id", type=str)
    acquire_parser.add_argument("--gpu-name", type=str, default=None)
    acquire_parser.add_argument("--num-gpus", type=int, default=1)
    acquire_parser.add_argument("--min-gpu-ram", type=float, default=None)
    acquire_parser.add_argument("--min-cpu-ram", type=float, default=None)
    acquire_parser.add_argument("--min-disk", type=float, default=None)
    acquire_parser.add_argument("--min-reliability", type=float, default=0.98)
    acquire_parser.add_argument("--estimated-hours-reference", type=float, default=2.0)
    acquire_parser.add_argument(
        "--image",
        type=str,
        default="nvidia/cuda:12.4.0-base-ubuntu22.04",
    )
    acquire_parser.add_argument("--disk-gb", type=int, default=60)
    acquire_parser.add_argument("--label-prefix", type=str, default="rezolve")
    acquire_parser.add_argument("--extra-filters", type=str, default=None)
    acquire_parser.add_argument(
        "--enable-idle-watchdog",
        action="store_true",
        help=(
            "Install the VM-side idle dead-man's switch. Self-destroys via Vast's "
            "per-instance CONTAINER_API_KEY; no manual API key needed."
        ),
    )
    acquire_parser.add_argument(
        "--idle-threshold-seconds",
        type=int,
        default=DEFAULT_IDLE_THRESHOLD_SECONDS,
        help="Idle seconds before self-destroy (production 3600; smoke 300).",
    )
    acquire_parser.add_argument(
        "--grace-seconds",
        type=int,
        default=DEFAULT_GRACE_SECONDS,
        help="Delay after boot before the watchdog arms (production 600; smoke 30).",
    )
    acquire_parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between GPU-utilization samples (default 60; smoke 20).",
    )

    teardown_parser: argparse.ArgumentParser = subparsers.add_parser(
        "teardown",
        help="Destroy a Vast.ai instance acquired for a task.",
    )
    teardown_parser.add_argument("task_id", type=str)
    teardown_parser.add_argument("--acquired-at", type=str, required=True)
    teardown_parser.add_argument("--instance-id", type=str, default=None)
    teardown_parser.add_argument("--price-per-hour", type=float, default=0.0)

    return parser


def _resolve_instance_id(
    *,
    sdk: Any,
    task_id: str,
    label_prefix: str,
) -> str | None:
    expected_label: str = f"{label_prefix}/{task_id}"
    try:
        raw_instances: Any = sdk.show_instances()
        instances: list[Any] = list(raw_instances)
    except Exception:  # noqa: BLE001 - best-effort lookup
        return None
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        if str(inst.get("label", "")) == expected_label:
            return str(inst.get("id", ""))
    return None


def _build_onstart(*, args: argparse.Namespace) -> str | None:
    if not args.enable_idle_watchdog:
        return None
    return render_vast_onstart(
        config=WatchdogConfig(
            idle_threshold_seconds=args.idle_threshold_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            grace_seconds=args.grace_seconds,
        ),
    )


def _run_acquire(*, args: argparse.Namespace) -> int:
    # Validate watchdog config before any SDK/network work so the error is fast.
    onstart: str | None = _build_onstart(args=args)
    sdk: Any = init_sdk()
    criteria: SearchCriteria = SearchCriteria(
        gpu_name=args.gpu_name,
        num_gpus=args.num_gpus,
        min_gpu_ram=args.min_gpu_ram,
        min_cpu_ram=args.min_cpu_ram,
        min_disk=args.min_disk,
        min_reliability=args.min_reliability,
        estimated_hours_reference=args.estimated_hours_reference,
        extra_filters=args.extra_filters,
    )
    label: str = f"{args.label_prefix}/{args.task_id}"
    provision_result: ProvisionResult = acquire(
        sdk=sdk,
        task_id=args.task_id,
        criteria=criteria,
        image=args.image,
        disk_gb=args.disk_gb,
        label=label,
        onstart=onstart,
    )
    entry: dict[str, Any] = to_machine_log_entry(provision_result=provision_result)
    print(json.dumps(entry, indent=2))
    return 0


def _run_teardown(*, args: argparse.Namespace) -> int:
    sdk: Any = init_sdk()
    instance_id: str | None = args.instance_id
    if instance_id is None:
        resolved: str | None = _resolve_instance_id(
            sdk=sdk,
            task_id=args.task_id,
            label_prefix="rezolve",
        )
        instance_id = resolved if resolved is not None and len(resolved) > 0 else args.task_id
    destroy_result: DestroyResult = destroy_and_confirm(
        sdk=sdk,
        instance_id=instance_id,
        created_at=args.acquired_at,
        price_per_hour=args.price_per_hour,
    )
    print(
        json.dumps(
            {
                "instance_id": destroy_result.instance_id,
                "destroyed_at": destroy_result.destroyed_at,
                "total_duration_hours": destroy_result.total_duration_hours,
                "total_cost_usd": destroy_result.total_cost_usd,
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = _build_argparser()
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        if args.command == "acquire":
            return _run_acquire(args=args)
        if args.command == "teardown":
            return _run_teardown(args=args)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except VastProvisioningError as exc:
        print(f"vast provisioning error: {exc}", file=sys.stderr)
        return 75
    except Exception as exc:  # noqa: BLE001 - CLI must map all errors
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
