"""Tests for arf.scripts.utils.nebius_machines module.

Tests written before implementation to define the expected contract for the
Nebius provider. The implementation in
``arf/scripts/utils/nebius_machines.py`` does not yet exist; every test in
this file is expected to fail with ``ModuleNotFoundError`` until the
implementation lands.

The patterns here mirror ``test_vast_machines.py``: synthetic CLI JSON
outputs, ``subprocess.run`` calls patched via ``monkeypatch``, and
``tmp_path`` redirection for the ``~/.arf-locks/`` directory so tests never
touch the real home directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Synthetic Nebius CLI JSON fixtures
# ---------------------------------------------------------------------------


def _platform_list_payload() -> dict[str, Any]:
    return {
        "items": [
            {
                "metadata": {"name": "gpu-h200-sxm"},
                "spec": {
                    "presets": [
                        {
                            "name": "1gpu-16vcpu-200gb",
                            "resources": {
                                "gpus": 1,
                                "gpu_memory_gibibytes": 141,
                                "vcpu_count": 16,
                                "ram_gibibytes": 200,
                            },
                        },
                        {
                            "name": "8gpu-128vcpu-1600gb",
                            "resources": {
                                "gpus": 8,
                                "gpu_memory_gibibytes": 1128,
                                "vcpu_count": 128,
                                "ram_gibibytes": 1600,
                            },
                        },
                    ],
                },
            },
        ],
    }


def _running_instance_payload(
    *,
    instance_id: str = "computeinstance-abc123",
    public_ip: str | None = "1.2.3.4",
    private_ip: str = "10.0.0.5",
    boot_disk_id: str = "computedisk-boot-9000",
    image_id: str = "computeimage-ubuntu-2204",
    region: str = "eu-north1",
    tenant_id: str = "tenant-rezolve",
    project_id: str = "project-arf",
) -> dict[str, Any]:
    return {
        "metadata": {
            "id": instance_id,
            "name": "nebius-test",
        },
        "status": {
            "state": "RUNNING",
            "network_interfaces": [
                {
                    "private_ip_address": {"address": private_ip},
                    "public_ip_address": (
                        {"address": public_ip} if public_ip is not None else None
                    ),
                },
            ],
        },
        "spec": {
            "boot_disk": {"existing_disk": {"id": boot_disk_id}},
            "resources": {
                "platform": "gpu-h200-sxm",
                "preset": "1gpu-16vcpu-200gb",
            },
            "image_id": image_id,
            "region": region,
            "tenant_id": tenant_id,
            "project_id": project_id,
        },
    }


# ---------------------------------------------------------------------------
# Subprocess monkeypatch helper
# ---------------------------------------------------------------------------


class _FakeRunResult:
    """Lightweight stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, *, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode: int = returncode
        self.stdout: str = stdout
        self.stderr: str = stderr


def _install_fake_run(
    *,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[list[str]], _FakeRunResult],
) -> list[list[str]]:
    """Replace ``subprocess.run`` inside the module with a recording fake.

    ``handler`` is a callable ``(cmd: list[str]) -> _FakeRunResult``.
    Returns the recorded list of commands for assertions.
    """
    recorded: list[list[str]] = []

    def fake_run(
        cmd: list[str],
        *args: Any,
        **kwargs: Any,
    ) -> _FakeRunResult:
        recorded.append(list(cmd))
        result: _FakeRunResult = handler(cmd)
        return result

    monkeypatch.setattr(
        "arf.scripts.utils.nebius_machines.subprocess.run",
        fake_run,
        raising=False,
    )
    return recorded


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    target: Path = tmp_path / "arf-locks"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def ssh_key(tmp_path: Path) -> Path:
    key: Path = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAAC3...example test@host\n", encoding="utf-8")
    return key


# ---------------------------------------------------------------------------
# Dataclass contract tests
# ---------------------------------------------------------------------------


def test_nebius_offer_dataclass_frozen() -> None:
    from arf.scripts.utils.nebius_machines import NebiusOffer

    offer: NebiusOffer = NebiusOffer(
        platform="gpu-h200-sxm",
        preset="1gpu-16vcpu-200gb",
        region="eu-north1",
        gpu_class="H200",
        gpu_count=1,
        gpu_memory_gibibytes=141,
        vcpu_count=16,
        ram_gibibytes=200,
        hourly_cost_estimate_usd=None,
    )
    with pytest.raises(FrozenInstanceError):
        offer.platform = "other"  # type: ignore[misc]


def test_acquire_result_dataclass_fields() -> None:
    from arf.scripts.utils.nebius_machines import AcquireResult

    result: AcquireResult = AcquireResult(
        instance_id="computeinstance-abc123",
        public_ip="1.2.3.4",
        private_ip="10.0.0.5",
        boot_disk_id="computedisk-boot-9000",
        region="eu-north1",
        platform="gpu-h200-sxm",
        preset="1gpu-16vcpu-200gb",
        image_id="computeimage-ubuntu-2204",
        acquired_at="2026-06-03T00:00:00+00:00",
        hourly_cost_estimate_usd=None,
        tenant_id="tenant-rezolve",
        project_id="project-arf",
    )
    assert result.instance_id == "computeinstance-abc123"
    assert result.public_ip == "1.2.3.4"
    assert result.private_ip == "10.0.0.5"
    with pytest.raises(FrozenInstanceError):
        result.public_ip = "9.9.9.9"  # type: ignore[misc]


def test_teardown_result_dataclass_fields() -> None:
    from arf.scripts.utils.nebius_machines import TeardownResult

    result: TeardownResult = TeardownResult(
        instance_id="computeinstance-abc123",
        terminated_at="2026-06-03T01:00:00+00:00",
        success=True,
        error_message=None,
        teardown_duration_seconds=12.5,
    )
    assert result.success is True
    assert result.error_message is None
    with pytest.raises(FrozenInstanceError):
        result.success = False  # type: ignore[misc]


def test_failed_attempt_dataclass_fields() -> None:
    from arf.scripts.utils.nebius_machines import FailedAttempt, NebiusOffer

    offer: NebiusOffer = NebiusOffer(
        platform="gpu-h200-sxm",
        preset="1gpu-16vcpu-200gb",
        region="eu-north1",
        gpu_class="H200",
        gpu_count=1,
        gpu_memory_gibibytes=141,
        vcpu_count=16,
        ram_gibibytes=200,
        hourly_cost_estimate_usd=None,
    )
    attempt: FailedAttempt = FailedAttempt(
        offer=offer,
        failure_reason="creation timeout",
        failed_at="2026-06-03T00:01:00+00:00",
        wasted_cost_usd=0.05,
    )
    assert attempt.offer.platform == "gpu-h200-sxm"
    with pytest.raises(FrozenInstanceError):
        attempt.failure_reason = "other"  # type: ignore[misc]


def test_provision_result_dataclass_fields() -> None:
    from arf.scripts.utils.nebius_machines import (
        AcquireResult,
        ProvisionResult,
    )

    acquire_result: AcquireResult = AcquireResult(
        instance_id="computeinstance-abc123",
        public_ip="1.2.3.4",
        private_ip="10.0.0.5",
        boot_disk_id="computedisk-boot-9000",
        region="eu-north1",
        platform="gpu-h200-sxm",
        preset="1gpu-16vcpu-200gb",
        image_id="computeimage-ubuntu-2204",
        acquired_at="2026-06-03T00:00:00+00:00",
        hourly_cost_estimate_usd=None,
        tenant_id="tenant-rezolve",
        project_id="project-arf",
    )
    result: ProvisionResult = ProvisionResult(
        acquire_result=acquire_result,
        failed_attempts=[],
        total_provisioning_seconds=120.0,
    )
    assert result.acquire_result is acquire_result
    assert result.failed_attempts == []
    assert result.total_provisioning_seconds == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# Exception class
# ---------------------------------------------------------------------------


def test_nebius_provisioning_error_is_exception() -> None:
    from arf.scripts.utils.nebius_machines import NebiusProvisioningError

    assert issubclass(NebiusProvisioningError, Exception)
    with pytest.raises(NebiusProvisioningError):
        raise NebiusProvisioningError("boom")


# ---------------------------------------------------------------------------
# init_sdk
# ---------------------------------------------------------------------------


def test_init_sdk_whoami_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import nebius_machines

    def handler(cmd: list[str]) -> _FakeRunResult:
        assert "whoami" in cmd
        assert "-p" in cmd
        return _FakeRunResult(
            returncode=0,
            stdout='{"id": "user-1"}',
            stderr="",
        )

    recorded: list[list[str]] = _install_fake_run(
        monkeypatch=monkeypatch,
        handler=handler,
    )
    nebius_machines.init_sdk(profile_name="compute")
    assert len(recorded) == 1
    assert recorded[0][0] == "nebius"
    assert "compute" in recorded[0]


def test_init_sdk_whoami_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import nebius_machines

    def handler(cmd: list[str]) -> _FakeRunResult:
        return _FakeRunResult(returncode=1, stdout="", stderr="not authenticated")

    _install_fake_run(monkeypatch=monkeypatch, handler=handler)
    with pytest.raises(nebius_machines.NebiusProvisioningError):
        nebius_machines.init_sdk(profile_name="compute")


# ---------------------------------------------------------------------------
# list_platforms
# ---------------------------------------------------------------------------


def test_list_platforms_parses_cli_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import nebius_machines

    payload: str = json.dumps(_platform_list_payload())

    def handler(cmd: list[str]) -> _FakeRunResult:
        assert "compute" in cmd
        assert "platform" in cmd
        assert "list" in cmd
        return _FakeRunResult(returncode=0, stdout=payload, stderr="")

    _install_fake_run(monkeypatch=monkeypatch, handler=handler)
    offers: list[nebius_machines.NebiusOffer] = nebius_machines.list_platforms(
        profile_name="compute",
    )
    assert len(offers) == 2
    first: nebius_machines.NebiusOffer = offers[0]
    assert first.platform == "gpu-h200-sxm"
    assert first.preset == "1gpu-16vcpu-200gb"
    assert first.gpu_count == 1
    assert first.gpu_memory_gibibytes == 141
    assert first.vcpu_count == 16
    assert first.ram_gibibytes == 200
    assert first.hourly_cost_estimate_usd is None


def test_list_platforms_cli_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import nebius_machines

    def handler(cmd: list[str]) -> _FakeRunResult:
        return _FakeRunResult(returncode=2, stdout="", stderr="permission denied")

    _install_fake_run(monkeypatch=monkeypatch, handler=handler)
    with pytest.raises(nebius_machines.NebiusProvisioningError):
        nebius_machines.list_platforms(profile_name="compute")


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------


def _make_acquire_handler(
    *,
    create_returncode: int = 0,
    final_state: str = "RUNNING",
    create_stdout: str | None = None,
    public_ip: str | None = "1.2.3.4",
) -> Any:
    """Return a subprocess.run handler that simulates the Nebius CLI."""
    if create_stdout is None:
        create_stdout = json.dumps(
            {"metadata": {"id": "computeinstance-abc123"}},
        )
    running_payload: str = json.dumps(
        _running_instance_payload(public_ip=public_ip),
    )

    def handler(cmd: list[str]) -> _FakeRunResult:
        if "create" in cmd:
            return _FakeRunResult(
                returncode=create_returncode,
                stdout=create_stdout,
                stderr="" if create_returncode == 0 else "create failed",
            )
        if "get" in cmd or "describe" in cmd:
            if final_state != "RUNNING":
                payload: dict[str, Any] = json.loads(running_payload)
                payload["status"]["state"] = final_state
                return _FakeRunResult(
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            return _FakeRunResult(returncode=0, stdout=running_payload, stderr="")
        if "delete" in cmd or "terminate" in cmd:
            return _FakeRunResult(returncode=0, stdout="{}", stderr="")
        return _FakeRunResult(returncode=0, stdout="{}", stderr="")

    return handler


def test_acquire_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    lock_dir: Path,
    ssh_key: Path,
) -> None:
    from arf.scripts.utils import nebius_machines

    _install_fake_run(
        monkeypatch=monkeypatch,
        handler=_make_acquire_handler(),
    )

    def fake_smoke_gate(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        nebius_machines,
        "_wait_for_ssh_reachable",
        fake_smoke_gate,
        raising=False,
    )

    result: nebius_machines.ProvisionResult = nebius_machines.acquire(
        task_id="t0099_demo",
        gpu_class="H200",
        preset="1gpu-16vcpu-200gb",
        image_id="computeimage-ubuntu-2204",
        ssh_public_key_path=ssh_key,
        profile_name="compute",
        region="eu-north1",
        tenant_id="tenant-rezolve",
        project_id="project-arf",
        subnet_id="subnet-1",
        boot_disk_size_gibibytes=100,
        name="nebius-test",
        max_wait_seconds=600,
        ssh_smoke_gate_timeout_seconds=120,
        lock_dir=lock_dir,
    )
    assert result.acquire_result is not None
    assert result.acquire_result.instance_id == "computeinstance-abc123"
    assert result.acquire_result.platform == "gpu-h200-sxm"
    assert (lock_dir / "t0099_demo.lock").exists()


def test_acquire_ssh_smoke_gate_failure_tears_down(
    monkeypatch: pytest.MonkeyPatch,
    lock_dir: Path,
    ssh_key: Path,
) -> None:
    from arf.scripts.utils import nebius_machines

    recorded: list[list[str]] = _install_fake_run(
        monkeypatch=monkeypatch,
        handler=_make_acquire_handler(),
    )

    def fake_smoke_gate(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(
        nebius_machines,
        "_wait_for_ssh_reachable",
        fake_smoke_gate,
        raising=False,
    )

    with pytest.raises(nebius_machines.NebiusProvisioningError):
        nebius_machines.acquire(
            task_id="t0099_demo",
            gpu_class="H200",
            preset="1gpu-16vcpu-200gb",
            image_id="computeimage-ubuntu-2204",
            ssh_public_key_path=ssh_key,
            profile_name="compute",
            region="eu-north1",
            tenant_id="tenant-rezolve",
            project_id="project-arf",
            subnet_id="subnet-1",
            lock_dir=lock_dir,
        )
    # At least one teardown-style invocation should have happened.
    flat: list[str] = [token for cmd in recorded for token in cmd]
    assert any(token in {"delete", "terminate"} for token in flat)


def test_acquire_lock_collision_raises(
    monkeypatch: pytest.MonkeyPatch,
    lock_dir: Path,
    ssh_key: Path,
) -> None:
    from arf.scripts.utils import nebius_machines

    (lock_dir / "t0099_demo.lock").write_text(
        "held by another task\n",
        encoding="utf-8",
    )

    _install_fake_run(
        monkeypatch=monkeypatch,
        handler=_make_acquire_handler(),
    )

    def fake_smoke_gate(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        nebius_machines,
        "_wait_for_ssh_reachable",
        fake_smoke_gate,
        raising=False,
    )

    with pytest.raises(nebius_machines.NebiusProvisioningError):
        nebius_machines.acquire(
            task_id="t0099_demo",
            gpu_class="H200",
            preset="1gpu-16vcpu-200gb",
            image_id="computeimage-ubuntu-2204",
            ssh_public_key_path=ssh_key,
            profile_name="compute",
            region="eu-north1",
            tenant_id="tenant-rezolve",
            project_id="project-arf",
            subnet_id="subnet-1",
            lock_dir=lock_dir,
        )


def test_acquire_create_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
    lock_dir: Path,
    ssh_key: Path,
) -> None:
    from arf.scripts.utils import nebius_machines

    _install_fake_run(
        monkeypatch=monkeypatch,
        handler=_make_acquire_handler(create_returncode=1),
    )

    def fake_smoke_gate(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        nebius_machines,
        "_wait_for_ssh_reachable",
        fake_smoke_gate,
        raising=False,
    )

    with pytest.raises(nebius_machines.NebiusProvisioningError):
        nebius_machines.acquire(
            task_id="t0099_demo",
            gpu_class="H200",
            preset="1gpu-16vcpu-200gb",
            image_id="computeimage-ubuntu-2204",
            ssh_public_key_path=ssh_key,
            profile_name="compute",
            region="eu-north1",
            tenant_id="tenant-rezolve",
            project_id="project-arf",
            subnet_id="subnet-1",
            lock_dir=lock_dir,
        )


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


def test_teardown_success_clears_lock(
    monkeypatch: pytest.MonkeyPatch,
    lock_dir: Path,
) -> None:
    from arf.scripts.utils import nebius_machines

    lock_path: Path = lock_dir / "t0099_demo.lock"
    lock_path.write_text("computeinstance-abc123\n", encoding="utf-8")

    def handler(cmd: list[str]) -> _FakeRunResult:
        assert "delete" in cmd or "terminate" in cmd
        return _FakeRunResult(returncode=0, stdout="{}", stderr="")

    _install_fake_run(monkeypatch=monkeypatch, handler=handler)
    result: nebius_machines.TeardownResult = nebius_machines.teardown(
        task_id="t0099_demo",
        instance_id="computeinstance-abc123",
        profile_name="compute",
        lock_dir=lock_dir,
    )
    assert result.success is True
    assert result.error_message is None
    assert result.instance_id == "computeinstance-abc123"
    assert not lock_path.exists()


def test_teardown_idempotent_when_instance_missing(
    monkeypatch: pytest.MonkeyPatch,
    lock_dir: Path,
) -> None:
    from arf.scripts.utils import nebius_machines

    lock_path: Path = lock_dir / "t0099_demo.lock"
    lock_path.write_text("computeinstance-abc123\n", encoding="utf-8")

    def handler(cmd: list[str]) -> _FakeRunResult:
        return _FakeRunResult(
            returncode=1,
            stdout="",
            stderr="NotFound: instance does not exist",
        )

    _install_fake_run(monkeypatch=monkeypatch, handler=handler)
    result: nebius_machines.TeardownResult = nebius_machines.teardown(
        task_id="t0099_demo",
        instance_id="computeinstance-abc123",
        profile_name="compute",
        lock_dir=lock_dir,
    )
    assert result.success is True
    assert not lock_path.exists()


def test_teardown_when_lock_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    lock_dir: Path,
) -> None:
    from arf.scripts.utils import nebius_machines

    def handler(cmd: list[str]) -> _FakeRunResult:
        return _FakeRunResult(returncode=0, stdout="{}", stderr="")

    _install_fake_run(monkeypatch=monkeypatch, handler=handler)
    result: nebius_machines.TeardownResult = nebius_machines.teardown(
        task_id="t0099_demo",
        instance_id="computeinstance-abc123",
        profile_name="compute",
        lock_dir=lock_dir,
    )
    assert result.success is True


# ---------------------------------------------------------------------------
# to_machine_log_entry
# ---------------------------------------------------------------------------


def _make_acquire_result() -> Any:
    from arf.scripts.utils.nebius_machines import AcquireResult

    return AcquireResult(
        instance_id="computeinstance-abc123",
        public_ip="1.2.3.4",
        private_ip="10.0.0.5",
        boot_disk_id="computedisk-boot-9000",
        region="eu-north1",
        platform="gpu-h200-sxm",
        preset="1gpu-16vcpu-200gb",
        image_id="computeimage-ubuntu-2204",
        acquired_at="2026-06-03T00:00:00+00:00",
        hourly_cost_estimate_usd=None,
        tenant_id="tenant-rezolve",
        project_id="project-arf",
    )


def test_to_machine_log_entry_provider_is_nebius() -> None:
    from arf.scripts.utils.nebius_machines import to_machine_log_entry

    entry: dict[str, object] = to_machine_log_entry(
        acquire_result=_make_acquire_result(),
        task_id="t0099_demo",
        gpu_class="H200",
        failed_attempts=[],
        total_provisioning_seconds=120.0,
    )
    assert entry["provider"] == "nebius"


def test_to_machine_log_entry_required_v5_fields() -> None:
    from arf.scripts.utils.nebius_machines import to_machine_log_entry

    entry: dict[str, object] = to_machine_log_entry(
        acquire_result=_make_acquire_result(),
        task_id="t0099_demo",
        gpu_class="H200",
        failed_attempts=[],
        total_provisioning_seconds=120.0,
    )
    required_fields: list[str] = [
        "spec_version",
        "provider",
        "instance_id",
        "platform",
        "preset",
        "image_id",
        "region",
        "tenant_id",
        "project_id",
        "public_ip",
        "boot_disk_id",
        "total_provisioning_seconds",
        "failed_attempts",
    ]
    for field in required_fields:
        assert field in entry, f"missing field: {field}"


def test_to_machine_log_entry_failed_attempts_serialised() -> None:
    from arf.scripts.utils.nebius_machines import (
        FailedAttempt,
        NebiusOffer,
        to_machine_log_entry,
    )

    offer: NebiusOffer = NebiusOffer(
        platform="gpu-h200-sxm",
        preset="1gpu-16vcpu-200gb",
        region="eu-north1",
        gpu_class="H200",
        gpu_count=1,
        gpu_memory_gibibytes=141,
        vcpu_count=16,
        ram_gibibytes=200,
        hourly_cost_estimate_usd=None,
    )
    failure: FailedAttempt = FailedAttempt(
        offer=offer,
        failure_reason="create timeout",
        failed_at="2026-06-03T00:01:00+00:00",
        wasted_cost_usd=0.05,
    )
    entry: dict[str, object] = to_machine_log_entry(
        acquire_result=_make_acquire_result(),
        task_id="t0099_demo",
        gpu_class="H200",
        failed_attempts=[failure],
        total_provisioning_seconds=180.0,
    )
    serialised: object = entry["failed_attempts"]
    assert isinstance(serialised, list)
    assert len(serialised) == 1


# ---------------------------------------------------------------------------
# CLI main
# ---------------------------------------------------------------------------


def test_main_callable_exists() -> None:
    from arf.scripts.utils import nebius_machines

    assert callable(getattr(nebius_machines, "main", None))


def test_cli_module_invokable_as_subprocess() -> None:
    """The module must be runnable via ``python -m arf.scripts.utils.nebius_machines``."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-m", "arf.scripts.utils.nebius_machines", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    combined: str = completed.stdout + completed.stderr
    assert "whoami" in combined
    assert "list" in combined
    assert "acquire" in combined
    assert "teardown" in combined


def test_cli_whoami_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import nebius_machines

    def fake_init(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(nebius_machines, "init_sdk", fake_init, raising=False)
    exit_code: int = nebius_machines.main(["whoami"])
    assert exit_code == 0


def test_cli_acquire_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ssh_key: Path,
    lock_dir: Path,
) -> None:
    from arf.scripts.utils import nebius_machines

    def fake_acquire(**kwargs: Any) -> nebius_machines.ProvisionResult:
        return nebius_machines.ProvisionResult(
            acquire_result=_make_acquire_result(),
            failed_attempts=[],
            total_provisioning_seconds=120.0,
        )

    def fake_init(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(nebius_machines, "acquire", fake_acquire, raising=False)
    monkeypatch.setattr(nebius_machines, "init_sdk", fake_init, raising=False)
    exit_code: int = nebius_machines.main(
        [
            "acquire",
            "t0099_demo",
            "--gpu-class",
            "H200",
            "--preset",
            "1gpu-16vcpu-200gb",
            "--image-id",
            "computeimage-ubuntu-2204",
            "--ssh-public-key-path",
            str(ssh_key),
            "--tenant-id",
            "tenant-rezolve",
            "--project-id",
            "project-arf",
            "--subnet-id",
            "subnet-1",
        ]
    )
    assert exit_code == 0
    out: str = capsys.readouterr().out
    payload: dict[str, Any] = json.loads(out)
    assert "acquire_result" in payload or "instance_id" in payload


def test_cli_teardown_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import nebius_machines

    def fake_teardown(**kwargs: Any) -> nebius_machines.TeardownResult:
        return nebius_machines.TeardownResult(
            instance_id="computeinstance-abc123",
            terminated_at="2026-06-03T01:00:00+00:00",
            success=True,
            error_message=None,
            teardown_duration_seconds=12.5,
        )

    def fake_init(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(nebius_machines, "teardown", fake_teardown, raising=False)
    monkeypatch.setattr(nebius_machines, "init_sdk", fake_init, raising=False)
    exit_code: int = nebius_machines.main(
        [
            "teardown",
            "t0099_demo",
            "--instance-id",
            "computeinstance-abc123",
        ]
    )
    assert exit_code == 0
