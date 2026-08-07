"""Tests for ``arf.scripts.utils.azure_ml_vm``.

The az CLI and SSH subprocess are monkey-patched so no real Azure or SSH
traffic happens. Each test sets up a per-VM fake response table and asserts
the orchestration logic picks the right VM, places the lock, falls back when
the primary is busy, and tears down only when no foreign locks remain.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from arf.scripts.utils import azure_ml_vm
from arf.scripts.utils.azure_ml_vm import (
    EXIT_OK,
    EXIT_POOL_BUSY,
    AcquireResult,
    CommandResult,
    PoolBusyError,
    VmPoolEntry,
    acquire,
    load_pool,
    teardown,
    to_machine_log_entry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


PRIMARY: VmPoolEntry = VmPoolEntry(
    name="FT-NC80-v3",
    workspace="finetuning-workspace",
    resource_group="rezolve-AI",
    ssh_host_alias="FT-NC80-v3",
    hourly_cost_usd=13.96,
    priority=1,
    notes="primary",
)

FALLBACK: VmPoolEntry = VmPoolEntry(
    name="FT-NC80-v2",
    workspace="finetuning-workspace",
    resource_group="rezolve-AI",
    ssh_host_alias="FT-NC80-v2",
    hourly_cost_usd=13.96,
    priority=2,
    notes="fallback",
)


@dataclass(slots=True)
class FakeWorld:
    az_state_by_vm: dict[str, str]
    ssh_ok_by_vm: dict[str, bool]
    locks_by_vm: dict[str, list[str]]
    az_calls: list[list[str]]
    ssh_calls: list[tuple[str, str]]


def _install_fakes(monkeypatch: pytest.MonkeyPatch, world: FakeWorld) -> None:
    def fake_run_az(*, args: list[str], timeout: float = 60.0) -> CommandResult:
        world.az_calls.append(args)
        if len(args) >= 3 and args[0] == "ml" and args[1] == "compute":
            verb: str = args[2]
            try:
                name_idx: int = args.index("--name") + 1
                vm_name: str = args[name_idx]
            except ValueError:
                return CommandResult(returncode=1, stdout="", stderr="bad args")
            if verb == "show":
                state: str = world.az_state_by_vm.get(vm_name, "Unknown")
                return CommandResult(
                    returncode=0,
                    stdout=json.dumps({"state": state}),
                    stderr="",
                )
            if verb == "start":
                world.az_state_by_vm[vm_name] = "Running"
                world.ssh_ok_by_vm[vm_name] = True
                return CommandResult(returncode=0, stdout="ok", stderr="")
            if verb == "stop":
                world.az_state_by_vm[vm_name] = "Stopped"
                return CommandResult(returncode=0, stdout="ok", stderr="")
        return CommandResult(returncode=1, stdout="", stderr="unhandled az call")

    def fake_run_ssh(
        *,
        host_alias: str,
        remote_command: str,
        timeout: float = 60.0,
        connect_timeout: float = 10.0,
    ) -> CommandResult:
        world.ssh_calls.append((host_alias, remote_command))
        if not world.ssh_ok_by_vm.get(host_alias, False):
            return CommandResult(returncode=255, stdout="", stderr="ssh refused")
        if remote_command == "true":
            return CommandResult(returncode=0, stdout="", stderr="")
        if "ls " in remote_command and ".lock$" in remote_command:
            locks: list[str] = world.locks_by_vm.get(host_alias, [])
            lines: str = "\n".join(f"{t}.lock" for t in locks)
            return CommandResult(returncode=0, stdout=lines, stderr="")
        if "printf" in remote_command and ".lock" in remote_command:
            # Lock placement: extract task_id from the path at the end.
            tokens: list[str] = remote_command.split()
            target: str = tokens[-1]
            task_id: str = Path(target).stem
            world.locks_by_vm.setdefault(host_alias, []).append(task_id)
            return CommandResult(returncode=0, stdout="", stderr="")
        if remote_command.startswith("rm -f"):
            path: str = remote_command.split()[-1]
            task_id_rm: str = Path(path).stem
            if host_alias in world.locks_by_vm and task_id_rm in world.locks_by_vm[host_alias]:
                world.locks_by_vm[host_alias].remove(task_id_rm)
            return CommandResult(returncode=0, stdout="", stderr="")
        if remote_command.startswith("pkill"):
            return CommandResult(returncode=0, stdout="", stderr="")
        if "arf-preflight" in remote_command:
            # The preflight script is shipped as the SSH command itself and its
            # stdout is parsed, so the catch-all below (empty stdout) would read as
            # a failed preflight and fail every acquire test here. Literal wire keys
            # on purpose — see the same note in test_remote_preflight.py.
            return CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "linger_enabled": True,
                        "persist_path": "/mnt/cache/persist",
                        "persist_writable": True,
                    },
                ),
                stderr="",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(azure_ml_vm, "_run_az", fake_run_az)
    monkeypatch.setattr(azure_ml_vm, "_run_ssh", fake_run_ssh)
    # Make all wait helpers return immediately by collapsing sleep.
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> FakeWorld:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running", "FT-NC80-v2": "Stopped"},
        ssh_ok_by_vm={"FT-NC80-v3": True, "FT-NC80-v2": False},
        locks_by_vm={"FT-NC80-v3": [], "FT-NC80-v2": []},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    return w


# ---------------------------------------------------------------------------
# load_pool
# ---------------------------------------------------------------------------


def test_load_pool_orders_by_priority(tmp_path: Path) -> None:
    cfg: Path = tmp_path / "azure_vm.json"
    cfg.write_text(
        json.dumps(
            {
                "spec_version": "1",
                "vms": [
                    {
                        "name": "B",
                        "workspace": "w",
                        "resource_group": "rg",
                        "ssh_host_alias": "B",
                        "hourly_cost_usd": 1.0,
                        "priority": 5,
                        "notes": "",
                    },
                    {
                        "name": "A",
                        "workspace": "w",
                        "resource_group": "rg",
                        "ssh_host_alias": "A",
                        "hourly_cost_usd": 1.0,
                        "priority": 1,
                        "notes": "",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    pool: list[VmPoolEntry] = load_pool(config_path=cfg)
    assert [v.name for v in pool] == ["A", "B"]


def test_repo_pool_config_loads() -> None:
    # The framework template ships project/azure_vm.json.example (committed),
    # not a real project/azure_vm.json (created per-fork). Validate the example
    # so the canonical pool schema is exercised in a fresh checkout.
    example_path: Path = azure_ml_vm.POOL_CONFIG_PATH.with_name("azure_vm.json.example")
    pool: list[VmPoolEntry] = load_pool(config_path=example_path)
    assert len(pool) >= 1
    assert pool[0].priority == 1
    assert all(v.hourly_cost_usd > 0.0 for v in pool)


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------


def test_acquire_uses_running_primary(world: FakeWorld) -> None:
    result: AcquireResult = acquire(task_id="test-smoke", pool=[PRIMARY, FALLBACK])
    assert result.vm.name == "FT-NC80-v3"
    assert result.started_vm is False
    assert "test-smoke" in world.locks_by_vm["FT-NC80-v3"]
    assert result.failed_attempts == []


def test_acquire_starts_stopped_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Stopped"},
        ssh_ok_by_vm={"FT-NC80-v3": False},
        locks_by_vm={"FT-NC80-v3": []},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    result: AcquireResult = acquire(task_id="t-start", pool=[PRIMARY])
    assert result.vm.name == "FT-NC80-v3"
    assert result.started_vm is True
    assert w.az_state_by_vm["FT-NC80-v3"] == "Running"
    assert "t-start" in w.locks_by_vm["FT-NC80-v3"]


def test_acquire_falls_back_when_primary_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running", "FT-NC80-v2": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True, "FT-NC80-v2": True},
        locks_by_vm={"FT-NC80-v3": ["other-task"], "FT-NC80-v2": []},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    result: AcquireResult = acquire(task_id="t-fb", pool=[PRIMARY, FALLBACK])
    assert result.vm.name == "FT-NC80-v2"
    assert len(result.failed_attempts) == 1
    assert result.failed_attempts[0].failure_phase == "lock_held"
    assert "t-fb" in w.locks_by_vm["FT-NC80-v2"]


def test_acquire_writes_intervention_when_all_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running", "FT-NC80-v2": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True, "FT-NC80-v2": True},
        locks_by_vm={"FT-NC80-v3": ["someone-else"], "FT-NC80-v2": ["another"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    intervention_dir: Path = tmp_path / "intervention"
    with pytest.raises(PoolBusyError):
        acquire(
            task_id="t-busy",
            pool=[PRIMARY, FALLBACK],
            intervention_dir=intervention_dir,
        )
    intervention_file: Path = intervention_dir / "pool_busy_ft-nc80-v2_ft-nc80-v3.md"
    assert intervention_file.exists()
    body: str = intervention_file.read_text(encoding="utf-8")
    assert "FT-NC80-v3" in body
    assert "FT-NC80-v2" in body
    assert "someone-else" in body


def test_acquire_keys_intervention_path_on_attempted_vm_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Regression for t0055: 4 parallel subagents provisioning different --vm-name-pinned VMs
    # for the same task_id must not overwrite each other's pool_busy record.
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running", "FT-NC80-v2": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True, "FT-NC80-v2": True},
        locks_by_vm={"FT-NC80-v3": ["t0054"], "FT-NC80-v2": ["t0052"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    intervention_dir: Path = tmp_path / "intervention"
    with pytest.raises(PoolBusyError):
        acquire(
            task_id="t0055",
            pool=[PRIMARY],
            intervention_dir=intervention_dir,
        )
    with pytest.raises(PoolBusyError):
        acquire(
            task_id="t0055",
            pool=[FALLBACK],
            intervention_dir=intervention_dir,
        )
    files: list[str] = sorted(p.name for p in intervention_dir.iterdir())
    assert len(files) == 2
    assert any("v3" in name.lower() for name in files)
    assert any("v2" in name.lower() for name in files)


def test_acquire_treats_existing_self_lock_as_acquirable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True},
        locks_by_vm={"FT-NC80-v3": ["t-self"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    result: AcquireResult = acquire(task_id="t-self", pool=[PRIMARY])
    assert result.vm.name == "FT-NC80-v3"
    assert result.failed_attempts == []


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


def test_acquire_stops_vm_it_started_after_discovering_stale_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Regression for t0055's intervention/setup_machines_ft-arf-weu-v1.md: a stopped VM with
    # a stale foreign lock gets started to check the lock, the lock is found, and the VM must
    # not be left running -- the caller should never have to stop it by hand.
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Stopped"},
        ssh_ok_by_vm={"FT-NC80-v3": False},
        locks_by_vm={"FT-NC80-v3": ["stale-task"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    with pytest.raises(PoolBusyError):
        acquire(task_id="t-new", pool=[PRIMARY], intervention_dir=tmp_path / "intervention")
    assert w.az_state_by_vm["FT-NC80-v3"] == "Stopped"


def test_acquire_leaves_vm_running_when_it_was_already_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A VM that was already running before this attempt touched it must never be stopped by a
    # failed acquire -- only VMs this attempt itself started are fair game to clean up.
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True},
        locks_by_vm={"FT-NC80-v3": ["someone-else"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    with pytest.raises(PoolBusyError):
        acquire(task_id="t-new", pool=[PRIMARY], intervention_dir=tmp_path / "intervention")
    assert w.az_state_by_vm["FT-NC80-v3"] == "Running"


def test_teardown_clears_lock_and_stops_when_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True},
        locks_by_vm={"FT-NC80-v3": ["t-down"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    result = teardown(task_id="t-down", deallocate=True, pool=[PRIMARY])
    assert result.deallocated is True
    assert result.other_locks_present is False
    assert w.az_state_by_vm["FT-NC80-v3"] == "Stopped"
    assert "t-down" not in w.locks_by_vm["FT-NC80-v3"]


def test_teardown_skips_stop_when_other_locks_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True},
        locks_by_vm={"FT-NC80-v3": ["t-down", "other-task"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    result = teardown(task_id="t-down", deallocate=True, pool=[PRIMARY])
    assert result.deallocated is False
    assert result.other_locks_present is True
    assert w.az_state_by_vm["FT-NC80-v3"] == "Running"
    assert "t-down" not in w.locks_by_vm["FT-NC80-v3"]
    assert "other-task" in w.locks_by_vm["FT-NC80-v3"]


def test_teardown_respects_no_deallocate(monkeypatch: pytest.MonkeyPatch) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True},
        locks_by_vm={"FT-NC80-v3": ["t-down"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    result = teardown(task_id="t-down", deallocate=False, pool=[PRIMARY])
    assert result.deallocated is False
    assert w.az_state_by_vm["FT-NC80-v3"] == "Running"


def test_teardown_starts_a_stopped_vm_to_clear_its_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression for t0055's t0054 stale-lock incident: a VM stopped outside azure_ml_vm (by
    # hand, or by the idle watchdog) must still be releasable through teardown() -- clearing the
    # on-VM lock file needs SSH, which needs the VM running.
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Stopped"},
        ssh_ok_by_vm={"FT-NC80-v3": False},
        locks_by_vm={"FT-NC80-v3": ["t-down"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    result = teardown(task_id="t-down", deallocate=True, vm=PRIMARY, pool=[PRIMARY])
    assert result.deallocated is True
    assert "t-down" not in w.locks_by_vm["FT-NC80-v3"]
    assert w.az_state_by_vm["FT-NC80-v3"] == "Stopped"


def test_teardown_raises_when_lock_clear_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A silently-swallowed SSH failure here is exactly the t0054 bug: teardown() must never
    # report success while the on-VM lock file is still present.
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True},
        locks_by_vm={"FT-NC80-v3": ["t-down"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    monkeypatch.setattr(azure_ml_vm, "clear_remote_lock", lambda **_: False)
    with pytest.raises(RuntimeError, match="failed to clear the lock"):
        teardown(task_id="t-down", deallocate=True, vm=PRIMARY, pool=[PRIMARY])


def test_cli_teardown_accepts_vm_name_to_bypass_ssh_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Without --vm-name, resolving the locked VM walks the pool over SSH, which cannot find a
    # lock on a VM that is currently stopped. --vm-name lets a caller (who already knows which
    # VM from its own acquire output) skip that discovery entirely.
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Stopped"},
        ssh_ok_by_vm={"FT-NC80-v3": False},
        locks_by_vm={"FT-NC80-v3": ["t-cli-down"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    monkeypatch.setattr(
        azure_ml_vm,
        "POOL_CONFIG_PATH",
        tmp_path / "project" / "azure_vm.json",
    )
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "azure_vm.json").write_text(
        json.dumps(
            {
                "spec_version": "1",
                "vms": [
                    {
                        "name": "FT-NC80-v3",
                        "workspace": "finetuning-workspace",
                        "resource_group": "rezolve-AI",
                        "ssh_host_alias": "FT-NC80-v3",
                        "hourly_cost_usd": 13.96,
                        "priority": 1,
                        "notes": "",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    exit_code: int = azure_ml_vm.main(
        ["teardown", "t-cli-down", "--vm-name", "FT-NC80-v3"],
    )
    assert exit_code == EXIT_OK
    assert "t-cli-down" not in w.locks_by_vm["FT-NC80-v3"]
    out: str = capsys.readouterr().out
    assert "FT-NC80-v3" in out


def test_teardown_computes_duration_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True},
        locks_by_vm={"FT-NC80-v3": ["t-cost"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    # Pin "now" to give a stable 2.0-hour delta.
    fixed_now: str = "2026-05-12T12:00:00Z"
    monkeypatch.setattr(azure_ml_vm, "_now_iso", lambda: fixed_now)
    result = teardown(
        task_id="t-cost",
        deallocate=True,
        pool=[PRIMARY],
        acquired_at="2026-05-12T10:00:00Z",
    )
    assert result.duration_hours == pytest.approx(2.0)
    assert result.total_cost_usd == pytest.approx(13.96 * 2.0)


# ---------------------------------------------------------------------------
# machine_log.json shaping (consumed by aggregate_machines)
# ---------------------------------------------------------------------------


def test_to_machine_log_entry_has_aggregator_fields(world: FakeWorld) -> None:
    result: AcquireResult = acquire(task_id="t-shape", pool=[PRIMARY, FALLBACK])
    entry: dict[str, Any] = to_machine_log_entry(acquire_result=result)
    # Fields the aggregator reads:
    assert entry["selected_offer"]["gpu"] == "2xH100"
    assert "total_provisioning_seconds" in entry
    assert isinstance(entry["failed_attempts"], list)
    # Schema fields the verificator reads:
    assert entry["provider"] == "azure-ml"
    assert entry["instance_id"] == "FT-NC80-v3"
    assert entry["ssh_host"] == "FT-NC80-v3"
    assert entry["destroyed_at"] is None
    assert entry["total_cost_usd"] is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_acquire_exits_pool_busy_when_all_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    w: FakeWorld = FakeWorld(
        az_state_by_vm={"FT-NC80-v3": "Running", "FT-NC80-v2": "Running"},
        ssh_ok_by_vm={"FT-NC80-v3": True, "FT-NC80-v2": True},
        locks_by_vm={"FT-NC80-v3": ["x"], "FT-NC80-v2": ["y"]},
        az_calls=[],
        ssh_calls=[],
    )
    _install_fakes(monkeypatch=monkeypatch, world=w)
    intervention_root: Path = tmp_path / "tasks" / "t-busy-cli" / "intervention"
    monkeypatch.setattr(
        azure_ml_vm,
        "POOL_CONFIG_PATH",
        tmp_path / "project" / "azure_vm.json",
    )
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "azure_vm.json").write_text(
        json.dumps(
            {
                "spec_version": "1",
                "vms": [
                    {
                        "name": "FT-NC80-v3",
                        "workspace": "finetuning-workspace",
                        "resource_group": "rezolve-AI",
                        "ssh_host_alias": "FT-NC80-v3",
                        "hourly_cost_usd": 13.96,
                        "priority": 1,
                        "notes": "",
                    },
                    {
                        "name": "FT-NC80-v2",
                        "workspace": "finetuning-workspace",
                        "resource_group": "rezolve-AI",
                        "ssh_host_alias": "FT-NC80-v2",
                        "hourly_cost_usd": 13.96,
                        "priority": 2,
                        "notes": "",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    exit_code: int = azure_ml_vm.main(["acquire", "t-busy-cli"])
    assert exit_code == EXIT_POOL_BUSY
    assert intervention_root.exists()
    err: str = capsys.readouterr().err
    assert "pool busy" in err
