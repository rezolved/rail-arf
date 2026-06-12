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
    WATCHDOG_REMOTE_PATH,
    WATCHDOG_SCRIPT_PATH,
    WatchdogConfig,
    build_nebius_terminate_cmd,
    build_vast_terminate_cmd,
    render_nebius_cloud_init,
    render_vast_onstart,
)

SSH_KEY: str = "ssh-ed25519 AAAAC3Nz test@arf"
SMOKE_THRESHOLD_SECONDS: int = 300


def _decode_b64_block(*, text: str) -> bytes:
    # Extract the longest base64-looking token and decode it.
    candidates: list[str] = [tok for tok in text.replace(",", " ").split() if len(tok) > 100]
    assert len(candidates) >= 1, "expected an embedded base64 blob"
    longest: str = max(candidates, key=len)
    return base64.b64decode(longest)


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
