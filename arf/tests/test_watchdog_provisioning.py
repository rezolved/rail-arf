"""Tests for arf.scripts.utils.watchdog_provisioning.

These cover the pure rendering of the idle dead-man's-switch into each provider's
startup hook. No provisioning happens; the goal is to lock the contract that the
single watchdog script body and the right self-terminate command reach the VM.
"""

import base64

from arf.scripts.utils.watchdog_provisioning import (
    DEFAULT_IDLE_THRESHOLD_SECONDS,
    VAST_CONTAINER_API_KEY_ENV,
    VAST_SELF_ID_ENV,
    WATCHDOG_BOOT_LOG,
    WATCHDOG_REMOTE_DIR,
    WATCHDOG_REMOTE_PATH,
    WATCHDOG_RUNTIME_LOG_DEFAULT,
    WATCHDOG_SCRIPT_PATH,
    WatchdogConfig,
    build_azure_ml_terminate_cmd,
    build_nebius_terminate_cmd,
    build_vast_terminate_cmd,
    render_azure_ml_install_script,
    render_nebius_cloud_init,
    render_vast_onstart,
)

SSH_KEY: str = "ssh-ed25519 AAAAC3Nz test@arf"
SMOKE_THRESHOLD_SECONDS: int = 300

AZURE_VM_NAME: str = "arf-nc80-weu-v1"
AZURE_RESOURCE_GROUP: str = "rg-finetuning-weu"
AZURE_WORKSPACE_NAME: str = "finetuning-workspace"

# Deliberately every value non-default, so an implementation that hardcodes the
# module defaults instead of reading the passed WatchdogConfig fails.
AZURE_THRESHOLD_SECONDS: int = 1800
AZURE_POLL_INTERVAL_SECONDS: int = 45
AZURE_IDLE_UTIL_PERCENT: int = 7
AZURE_GRACE_SECONDS: int = 900

# Any of these appearing in the terminate command means a static secret was
# embedded instead of the VM's managed identity.
CREDENTIAL_LITERAL_MARKERS: list[str] = [
    "--service-principal",
    "--password",
    "--client-secret",
    "client_secret",
    "--tenant",
    "AZURE_CLIENT_SECRET",
    "api-key",
]

# Observable guards that prevent a second watchdog racing the first one.
IDEMPOTENCE_GUARD_MARKERS: list[str] = [
    "pkill",
    "pgrep",
    "killall",
    ".pid",
    "pidfile",
    "systemctl stop",
]


def _decode_b64_block(*, text: str) -> bytes:
    # Extract the longest base64-looking token and decode it.
    candidates: list[str] = [tok for tok in text.replace(",", " ").split() if len(tok) > 100]
    assert len(candidates) >= 1, "expected an embedded base64 blob"
    longest: str = max(candidates, key=len)
    return base64.b64decode(longest)


def _azure_config() -> WatchdogConfig:
    return WatchdogConfig(
        idle_threshold_seconds=AZURE_THRESHOLD_SECONDS,
        poll_interval_seconds=AZURE_POLL_INTERVAL_SECONDS,
        idle_util_percent=AZURE_IDLE_UTIL_PERCENT,
        grace_seconds=AZURE_GRACE_SECONDS,
    )


def _render_azure_install_script(*, config: WatchdogConfig) -> str:
    return render_azure_ml_install_script(
        vm_name=AZURE_VM_NAME,
        resource_group=AZURE_RESOURCE_GROUP,
        workspace_name=AZURE_WORKSPACE_NAME,
        config=config,
    )


def _extract_terminate_cmd_value(*, script: str) -> str:
    marker: str = "TERMINATE_CMD="
    assert marker in script, "install script must set TERMINATE_CMD"
    tail: str = script.split(marker, maxsplit=1)[1]
    line: str = tail.splitlines()[0]
    return line.strip().rstrip("\\").strip().strip("'\"").strip()


def test_watchdog_config_defaults_to_production_threshold() -> None:
    config: WatchdogConfig = WatchdogConfig()
    assert config.idle_threshold_seconds == DEFAULT_IDLE_THRESHOLD_SECONDS
    assert config.idle_threshold_seconds == 3600


def test_vast_terminate_cmd_uses_container_key_and_yes_flag() -> None:
    cmd: str = build_vast_terminate_cmd()
    assert "vastai destroy instance" in cmd
    assert f"${VAST_SELF_ID_ENV}" in cmd
    # Self-destroy uses Vast's per-instance key, not a manually-created scoped key.
    assert f"${VAST_CONTAINER_API_KEY_ENV}" in cmd
    assert "$ARF_WATCHDOG_KEY" not in cmd
    # `-y` is mandatory: without it the destroy prompts [y/N] and aborts with no TTY,
    # so the watchdog can never self-terminate (caught in the first live smoke).
    assert " -y " in cmd or cmd.rstrip().endswith("-y")


def test_nebius_terminate_cmd_uses_metadata_token_and_stops_self() -> None:
    cmd: str = build_nebius_terminate_cmd(profile_name="compute")
    # Stop (deallocate), never delete — disk and warm install must survive.
    assert "compute instance stop" in cmd
    assert "instance delete" not in cmd
    # Auth via the metadata token, not a static secret.
    assert "Metadata-Flavor" in cmd
    assert "$TOKEN" in cmd
    assert "$SELF_ID" in cmd


def test_vast_onstart_embeds_real_script_body() -> None:
    onstart: str = render_vast_onstart(config=WatchdogConfig())
    decoded: bytes = _decode_b64_block(text=onstart)
    assert decoded == WATCHDOG_SCRIPT_PATH.read_bytes()


def test_vast_onstart_wires_threshold_and_background_launch() -> None:
    onstart: str = render_vast_onstart(
        config=WatchdogConfig(idle_threshold_seconds=SMOKE_THRESHOLD_SECONDS),
    )
    assert onstart.startswith("#!/bin/bash")
    # No manually-created key is injected; self-destroy uses CONTAINER_API_KEY.
    assert "ARF_WATCHDOG_KEY" not in onstart
    assert f"IDLE_THRESHOLD_SECONDS={SMOKE_THRESHOLD_SECONDS}" in onstart
    assert "TERMINATE_CMD=" in onstart
    assert f"chmod +x {WATCHDOG_REMOTE_PATH}" in onstart
    # Must launch in the background so onstart returns and the container proceeds.
    assert onstart.rstrip().endswith("&")


def test_nebius_cloud_init_authorizes_ssh_and_installs_service() -> None:
    cloud_init: str = render_nebius_cloud_init(
        ssh_public_key=SSH_KEY,
        profile_name="compute",
        config=WatchdogConfig(idle_threshold_seconds=SMOKE_THRESHOLD_SECONDS),
    )
    assert cloud_init.startswith("#cloud-config")
    assert SSH_KEY in cloud_init
    assert "arf-idle-watchdog.service" in cloud_init
    assert "systemctl enable --now arf-idle-watchdog.service" in cloud_init
    assert f"IDLE_THRESHOLD_SECONDS={SMOKE_THRESHOLD_SECONDS}" in cloud_init
    assert "compute instance stop" in cloud_init


def test_nebius_cloud_init_embeds_real_script_body() -> None:
    cloud_init: str = render_nebius_cloud_init(
        ssh_public_key=SSH_KEY,
        profile_name="compute",
        config=WatchdogConfig(),
    )
    decoded: bytes = _decode_b64_block(text=cloud_init)
    assert decoded == WATCHDOG_SCRIPT_PATH.read_bytes()


def test_azure_ml_terminate_cmd_stops_pool_vm_with_managed_identity() -> None:
    cmd: str = build_azure_ml_terminate_cmd(
        vm_name=AZURE_VM_NAME,
        resource_group=AZURE_RESOURCE_GROUP,
        workspace_name=AZURE_WORKSPACE_NAME,
    )
    # Stop, never delete — the pool VM is a reusable resource shared across tasks.
    assert "az ml compute stop" in cmd
    assert "az ml compute delete" not in cmd
    assert "delete" not in cmd
    assert AZURE_VM_NAME in cmd
    assert AZURE_RESOURCE_GROUP in cmd
    assert AZURE_WORKSPACE_NAME in cmd
    # Auth via the VM's managed identity, not a static secret on disk.
    assert "az login --identity" in cmd
    for marker in CREDENTIAL_LITERAL_MARKERS:
        assert marker not in cmd, f"static credential literal {marker!r} embedded in terminate cmd"


def test_azure_ml_install_script_embeds_real_script_body() -> None:
    script: str = _render_azure_install_script(config=WatchdogConfig())
    decoded: bytes = _decode_b64_block(text=script)
    assert decoded == WATCHDOG_SCRIPT_PATH.read_bytes()


def test_azure_ml_install_script_wires_full_config_and_detached_launch() -> None:
    script: str = _render_azure_install_script(config=_azure_config())
    assert script.startswith("#!/bin/bash")
    # Every value comes from the passed config, not the module defaults.
    assert f"IDLE_THRESHOLD_SECONDS={AZURE_THRESHOLD_SECONDS}" in script
    assert f"POLL_INTERVAL_SECONDS={AZURE_POLL_INTERVAL_SECONDS}" in script
    assert f"IDLE_UTIL_PERCENT={AZURE_IDLE_UTIL_PERCENT}" in script
    assert f"GRACE_SECONDS={AZURE_GRACE_SECONDS}" in script
    assert f"IDLE_THRESHOLD_SECONDS={DEFAULT_IDLE_THRESHOLD_SECONDS}" not in script
    # TERMINATE_CMD is the Azure stop command.
    assert "az ml compute stop" in _extract_terminate_cmd_value(script=script)
    assert f"chmod +x {WATCHDOG_REMOTE_PATH}" in script
    # Runs over SSH on an already-running VM, so the watchdog must outlive the
    # SSH session: detached launch, not a foreground process.
    assert "nohup" in script or "setsid" in script or "systemctl enable --now" in script


def test_azure_ml_install_script_is_idempotent() -> None:
    script: str = _render_azure_install_script(config=_azure_config())
    # The same pool VM is acquired by many tasks; a re-run must not leave two
    # watchdogs racing, so an existing one has to be stopped or detected first.
    guards: list[str] = [marker for marker in IDEMPOTENCE_GUARD_MARKERS if marker in script]
    assert len(guards) >= 1, "install script has no guard against a second watchdog"
    # The guard must precede the launch, otherwise it kills the watchdog it just started.
    launch_index: int = script.rindex(WATCHDOG_REMOTE_PATH)
    guard_index: int = min(script.index(marker) for marker in guards)
    assert guard_index < launch_index


def test_azure_ml_install_script_creates_opt_and_var_log_sudo_safe() -> None:
    # S-0055-05 (t0055): /opt and /var/log ownership varies across Azure ML pool
    # VMs. On FT-ARF-weu-v1/weu-v2, the unprivileged `mkdir -p /opt/arf` and the
    # log-file writes both failed with "Permission denied" because azureuser did
    # not own those paths, and had to be worked around by hand with sudo each time.
    script: str = _render_azure_install_script(config=WatchdogConfig())

    # Try unprivileged first (works on VMs where azureuser already owns the
    # paths), fall back to non-interactive sudo. `sudo -n` never prompts, so a
    # VM with no passwordless sudo fails fast instead of hanging with no TTY.
    assert f"mkdir -p {WATCHDOG_REMOTE_DIR}" in script
    assert f"sudo -n mkdir -p {WATCHDOG_REMOTE_DIR}" in script
    assert f"sudo -n touch {WATCHDOG_BOOT_LOG} {WATCHDOG_RUNTIME_LOG_DEFAULT}" in script

    # Every sudo call must be non-interactive.
    assert script.count("sudo ") == script.count("sudo -n ")

    # chown only runs when the unprivileged path left something unwritable —
    # confirm both the directory and both log targets are covered.
    assert f"[ -w {WATCHDOG_REMOTE_DIR} ]" in script
    for target in (WATCHDOG_BOOT_LOG, WATCHDOG_RUNTIME_LOG_DEFAULT):
        assert f'sudo -n chown "$(whoami)":"$(whoami)" "{target}"' in script

    # The sudo-safe setup must run before anything writes into those paths.
    setup_index: int = script.index(f"mkdir -p {WATCHDOG_REMOTE_DIR}")
    write_index: int = script.index("base64 -d >")
    assert setup_index < write_index


def test_azure_ml_install_script_always_sets_non_empty_terminate_cmd() -> None:
    # A watchdog that cannot terminate is worse than none: it reports
    # watchdog_active while protecting nothing, and idle_watchdog.sh refuses to
    # run without TERMINATE_CMD. It must never be rendered empty.
    for config in [WatchdogConfig(), _azure_config()]:
        script: str = _render_azure_install_script(config=config)
        terminate_cmd: str = _extract_terminate_cmd_value(script=script)
        assert len(terminate_cmd) > 0
