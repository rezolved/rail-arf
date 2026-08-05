"""Tests for the remote-VM preflight guarantees (``LESSONS.md`` Lessons 10 and 11).

Two layers are covered here:

* ``arf/scripts/utils/remote_preflight.sh`` — a standalone bash script run on the GPU VM
  over SSH. It enables systemd lingering so a detached ``tmux`` job survives SSH
  disconnect, and guarantees the persistent-storage symlink resolves to the real Azure
  Files mount rather than the ephemeral ``/mnt`` temp disk. Exercised by running the real
  script in a ``tmp_path`` sandbox with a stub ``loginctl`` on ``PATH`` and the two
  documented env-var overrides (``ARF_PERSIST_LINK``, ``ARF_PERSIST_MOUNT_ROOT``).
* ``arf.scripts.utils.azure_ml_vm.run_remote_preflight`` — ships that script to the VM
  through the module's ``_run_ssh`` shim and parses its single JSON line, plus the
  ``acquire`` wiring that refuses to lock a VM whose preflight did not pass.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from arf.scripts.utils import azure_ml_vm
from arf.scripts.utils.azure_ml_vm import (
    AcquireResult,
    CommandResult,
    VmPoolEntry,
    acquire,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
PREFLIGHT_SCRIPT_PATH: Path = REPO_ROOT / "arf" / "scripts" / "utils" / "remote_preflight.sh"

PERSIST_LINK_ENV: str = "ARF_PERSIST_LINK"
PERSIST_MOUNT_ROOT_ENV: str = "ARF_PERSIST_MOUNT_ROOT"

LINGER_ENABLED_KEY: str = "linger_enabled"
PERSIST_PATH_KEY: str = "persist_path"
PERSIST_WRITABLE_KEY: str = "persist_writable"

BASH_AVAILABLE: bool = shutil.which("bash") is not None
RUNNING_AS_ROOT: bool = hasattr(os, "geteuid") and os.geteuid() == 0

LOGINCTL_LINGER_YES: str = """#!/bin/bash
if [ "$1" = "show-user" ]; then
  echo "UID=1000"
  echo "Linger=yes"
  exit 0
fi
exit 0
"""

LOGINCTL_LINGER_NO: str = """#!/bin/bash
if [ "$1" = "show-user" ]; then
  echo "UID=1000"
  echo "Linger=no"
  exit 0
fi
exit 1
"""

SUDO_PASSTHROUGH: str = """#!/bin/bash
exec "$@"
"""


# ---------------------------------------------------------------------------
# Sandbox helpers for the bash script
# ---------------------------------------------------------------------------


def _write_executable(*, path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _make_stub_bin(*, tmp_path: Path, loginctl_body: str) -> Path:
    bin_dir: Path = tmp_path / "stubbin"
    _write_executable(path=bin_dir / "loginctl", content=loginctl_body)
    # The script falls back to `sudo loginctl enable-linger`; the passthrough stub keeps
    # that fallback on the stubbed loginctl instead of escalating for real.
    _write_executable(path=bin_dir / "sudo", content=SUDO_PASSTHROUGH)
    return bin_dir


def _run_preflight(
    *,
    tmp_path: Path,
    persist_link: Path,
    mount_root: Path,
    loginctl_body: str = LOGINCTL_LINGER_YES,
) -> subprocess.CompletedProcess[str]:
    assert PREFLIGHT_SCRIPT_PATH.exists(), (
        f"remote_preflight.sh must exist at {PREFLIGHT_SCRIPT_PATH}"
    )
    bin_dir: Path = _make_stub_bin(tmp_path=tmp_path, loginctl_body=loginctl_body)
    env: dict[str, str] = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env[PERSIST_LINK_ENV] = str(persist_link)
    env[PERSIST_MOUNT_ROOT_ENV] = str(mount_root)
    return subprocess.run(
        ["bash", str(PREFLIGHT_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _parse_single_json_line(*, stdout: str) -> dict[str, object]:
    lines: list[str] = [line for line in stdout.splitlines() if len(line.strip()) > 0]
    assert len(lines) == 1, f"expected exactly one line of JSON on stdout; got {stdout!r}"
    payload: object = json.loads(lines[0])
    assert isinstance(payload, dict), "the emitted JSON line is an object"
    return payload


def _assert_stdout_is_clean_on_failure(*, stdout: str) -> None:
    # A failing run may print nothing, but must never print half a JSON object: whatever
    # reaches stdout has to stay parseable by the caller.
    stripped: str = stdout.strip()
    if len(stripped) == 0:
        return
    json.loads(stripped)


def _make_sandbox(*, tmp_path: Path) -> Path:
    # The link's parent must exist; the script repairs the link itself, not the tree
    # above it.
    link_parent: Path = tmp_path / "cache"
    link_parent.mkdir(parents=True, exist_ok=True)
    return link_parent


def _make_mount_root(*, tmp_path: Path) -> Path:
    mount_root: Path = tmp_path / "azurefiles" / "clusters" / "ft-nc80-v1" / "code"
    mount_root.mkdir(parents=True, exist_ok=True)
    return mount_root


# ---------------------------------------------------------------------------
# CONTRACT 1: remote_preflight.sh
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash is not available on this machine")
def test_preflight_success_emits_single_json_line(tmp_path: Path) -> None:
    mount_root: Path = _make_mount_root(tmp_path=tmp_path)
    persist_link: Path = _make_sandbox(tmp_path=tmp_path) / "persist"

    result: subprocess.CompletedProcess[str] = _run_preflight(
        tmp_path=tmp_path,
        persist_link=persist_link,
        mount_root=mount_root,
    )

    assert result.returncode == 0, f"preflight must succeed; stderr={result.stderr!r}"
    payload: dict[str, object] = _parse_single_json_line(stdout=result.stdout)
    assert payload[LINGER_ENABLED_KEY] is True
    assert payload[PERSIST_WRITABLE_KEY] is True
    persist_path: object = payload[PERSIST_PATH_KEY]
    assert isinstance(persist_path, str), "persist_path is a string"
    assert Path(persist_path).resolve() == mount_root.resolve()


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash is not available on this machine")
def test_preflight_fails_when_mount_root_missing(tmp_path: Path) -> None:
    # The Azure Files share is not mounted: writing checkpoints anyway is the silent
    # data loss of Lesson 10, so the script must refuse rather than create the tree.
    mount_root: Path = tmp_path / "azurefiles" / "never-mounted" / "code"
    persist_link: Path = _make_sandbox(tmp_path=tmp_path) / "persist"

    result: subprocess.CompletedProcess[str] = _run_preflight(
        tmp_path=tmp_path,
        persist_link=persist_link,
        mount_root=mount_root,
    )

    assert result.returncode != 0, "a missing mount root must fail preflight"
    assert len(result.stderr.strip()) > 0, "the failing guarantee must be named on stderr"
    assert "persist" in result.stderr.lower(), (
        f"stderr must name the persistent-storage guarantee; got {result.stderr!r}"
    )
    _assert_stdout_is_clean_on_failure(stdout=result.stdout)


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash is not available on this machine")
def test_preflight_repairs_symlink_pointing_at_ephemeral_target(tmp_path: Path) -> None:
    mount_root: Path = _make_mount_root(tmp_path=tmp_path)
    ephemeral: Path = tmp_path / "ephemeral" / "persist"
    ephemeral.mkdir(parents=True, exist_ok=True)
    persist_link: Path = _make_sandbox(tmp_path=tmp_path) / "persist"
    persist_link.symlink_to(ephemeral)

    result: subprocess.CompletedProcess[str] = _run_preflight(
        tmp_path=tmp_path,
        persist_link=persist_link,
        mount_root=mount_root,
    )

    assert result.returncode == 0, f"a wrong symlink must be repaired; stderr={result.stderr!r}"
    assert persist_link.resolve() == mount_root.resolve(), (
        "the link must now resolve to the real mount root, not the ephemeral disk"
    )
    payload: dict[str, object] = _parse_single_json_line(stdout=result.stdout)
    persist_path: object = payload[PERSIST_PATH_KEY]
    assert isinstance(persist_path, str), "persist_path is a string"
    assert Path(persist_path).resolve() == mount_root.resolve()


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash is not available on this machine")
@pytest.mark.skipif(RUNNING_AS_ROOT, reason="root bypasses directory write permissions")
def test_preflight_fails_when_link_is_not_writable(tmp_path: Path) -> None:
    mount_root: Path = _make_mount_root(tmp_path=tmp_path)
    persist_link: Path = _make_sandbox(tmp_path=tmp_path) / "persist"
    persist_link.symlink_to(mount_root)
    mount_root.chmod(0o500)
    try:
        result: subprocess.CompletedProcess[str] = _run_preflight(
            tmp_path=tmp_path,
            persist_link=persist_link,
            mount_root=mount_root,
        )
    finally:
        mount_root.chmod(0o700)

    assert result.returncode != 0, "an unwritable persist link must fail preflight"
    assert len(result.stderr.strip()) > 0, "the failing guarantee must be named on stderr"
    _assert_stdout_is_clean_on_failure(stdout=result.stdout)


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash is not available on this machine")
def test_preflight_fails_when_linger_cannot_be_verified(tmp_path: Path) -> None:
    # tmux alone does not survive SSH disconnect without Linger=yes (Lesson 11), so an
    # unverifiable linger is a hard failure, not a warning.
    mount_root: Path = _make_mount_root(tmp_path=tmp_path)
    persist_link: Path = _make_sandbox(tmp_path=tmp_path) / "persist"

    result: subprocess.CompletedProcess[str] = _run_preflight(
        tmp_path=tmp_path,
        persist_link=persist_link,
        mount_root=mount_root,
        loginctl_body=LOGINCTL_LINGER_NO,
    )

    assert result.returncode != 0, "Linger=no must fail preflight"
    assert "linger" in result.stderr.lower(), (
        f"stderr must name the linger guarantee; got {result.stderr!r}"
    )
    _assert_stdout_is_clean_on_failure(stdout=result.stdout)


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash is not available on this machine")
def test_preflight_is_idempotent(tmp_path: Path) -> None:
    mount_root: Path = _make_mount_root(tmp_path=tmp_path)
    persist_link: Path = _make_sandbox(tmp_path=tmp_path) / "persist"

    first: subprocess.CompletedProcess[str] = _run_preflight(
        tmp_path=tmp_path,
        persist_link=persist_link,
        mount_root=mount_root,
    )
    second: subprocess.CompletedProcess[str] = _run_preflight(
        tmp_path=tmp_path,
        persist_link=persist_link,
        mount_root=mount_root,
    )

    assert first.returncode == 0, f"first run must succeed; stderr={first.stderr!r}"
    assert second.returncode == 0, f"second run must succeed; stderr={second.stderr!r}"
    assert _parse_single_json_line(stdout=second.stdout)[PERSIST_WRITABLE_KEY] is True


# ---------------------------------------------------------------------------
# CONTRACT 2: run_remote_preflight + acquire wiring in azure_ml_vm
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

REMOTE_PERSIST_PATH: str = "/mnt/batch/tasks/shared/LS_root/mounts/clusters/ft-nc80-v3/code"
PREFLIGHT_FAILURE_PHASE: str = "preflight"

PREFLIGHT_OK_JSON: str = json.dumps(
    {
        # Literal keys on purpose: this is the wire format the bash script prints, not
        # an internal constant. Keying the fixture off the parser's own constants would
        # make any rename self-consistent and green while the two halves drifted apart.
        "linger_enabled": True,
        "persist_path": REMOTE_PERSIST_PATH,
        "persist_writable": True,
    },
)


def _patch_ssh_result(*, monkeypatch: pytest.MonkeyPatch, result: CommandResult) -> None:
    def fake_run_ssh(
        *,
        host_alias: str,
        remote_command: str,
        timeout: float = 60.0,
        connect_timeout: float = 10.0,
    ) -> CommandResult:
        return result

    monkeypatch.setattr(azure_ml_vm, "_run_ssh", fake_run_ssh)


def test_run_remote_preflight_parses_json_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ssh_result(
        monkeypatch=monkeypatch,
        result=CommandResult(returncode=0, stdout=PREFLIGHT_OK_JSON + "\n", stderr=""),
    )

    outcome = azure_ml_vm.run_remote_preflight(vm=PRIMARY)

    assert outcome is not None, "a successful preflight must return a PreflightResult"
    assert outcome.linger_enabled is True
    assert outcome.persist_path == REMOTE_PERSIST_PATH
    assert outcome.persist_writable is True


def test_run_remote_preflight_returns_none_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ssh_result(
        monkeypatch=monkeypatch,
        result=CommandResult(returncode=1, stdout="", stderr="persist link unwritable"),
    )

    assert azure_ml_vm.run_remote_preflight(vm=PRIMARY) is None


def test_run_remote_preflight_returns_none_on_unparseable_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ssh_result(
        monkeypatch=monkeypatch,
        result=CommandResult(returncode=0, stdout="Welcome to Ubuntu\nnot json\n", stderr=""),
    )

    assert azure_ml_vm.run_remote_preflight(vm=PRIMARY) is None


@pytest.mark.parametrize(
    "missing_key",
    [
        "linger_enabled",
        "persist_path",
        "persist_writable",
    ],
)
def test_run_remote_preflight_returns_none_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
) -> None:
    payload: dict[str, object] = json.loads(PREFLIGHT_OK_JSON)
    del payload[missing_key]
    _patch_ssh_result(
        monkeypatch=monkeypatch,
        result=CommandResult(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    assert azure_ml_vm.run_remote_preflight(vm=PRIMARY) is None


def _patch_acquire_world(
    *,
    monkeypatch: pytest.MonkeyPatch,
    preflight_failing_vm_name: str,
    placed_locks: list[str],
) -> None:
    # "running" is the module's normalized state string; the VM is already up so no
    # start/poll path runs and nothing sleeps.
    monkeypatch.setattr(azure_ml_vm, "get_compute_state", lambda *, vm: "running")
    monkeypatch.setattr(azure_ml_vm, "_ssh_ok", lambda *, vm: True)
    monkeypatch.setattr(azure_ml_vm, "list_remote_locks", lambda *, vm: [])

    def fake_place_remote_lock(*, vm: VmPoolEntry, task_id: str) -> None:
        placed_locks.append(vm.name)

    monkeypatch.setattr(azure_ml_vm, "place_remote_lock", fake_place_remote_lock)

    def fake_preflight(*, vm: VmPoolEntry) -> object | None:
        if vm.name == preflight_failing_vm_name:
            return None
        passed: object = azure_ml_vm.PreflightResult(
            linger_enabled=True,
            persist_path=REMOTE_PERSIST_PATH,
            persist_writable=True,
        )
        return passed

    monkeypatch.setattr(azure_ml_vm, "run_remote_preflight", fake_preflight)


def test_acquire_records_preflight_failure_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    placed_locks: list[str] = []
    _patch_acquire_world(
        monkeypatch=monkeypatch,
        preflight_failing_vm_name=PRIMARY.name,
        placed_locks=placed_locks,
    )

    result: AcquireResult = acquire(task_id="t-preflight", pool=[PRIMARY, FALLBACK])

    assert result.vm.name == FALLBACK.name, "a failed preflight must move on to the next VM"
    assert len(result.failed_attempts) == 1
    assert result.failed_attempts[0].failure_phase == PREFLIGHT_FAILURE_PHASE
    assert result.failed_attempts[0].vm_name == PRIMARY.name


def test_acquire_places_no_lock_when_preflight_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    placed_locks: list[str] = []
    _patch_acquire_world(
        monkeypatch=monkeypatch,
        preflight_failing_vm_name=PRIMARY.name,
        placed_locks=placed_locks,
    )

    acquire(task_id="t-preflight-lock", pool=[PRIMARY, FALLBACK])

    assert PRIMARY.name not in placed_locks, (
        "a VM whose preflight failed must not be locked — the lock would strand the task "
        f"on an unusable box; locks placed: {placed_locks}"
    )
    assert placed_locks == [FALLBACK.name]
