"""Embed ``idle_watchdog.sh`` into provider startup hooks.

The same watchdog script body (``idle_watchdog.sh``) runs on every provider; only
the self-terminate command and the way credentials reach the VM differ:

* Vast.ai — the watchdog and a **scoped** API key (``manage_instances`` only, no
  billing) are injected via the instance ``onstart`` script. The instance knows
  its own contract id through the ``CONTAINER_ID`` container env var.
* Nebius — the watchdog is installed via cloud-init as a systemd service. The
  instance authenticates with its attached **service account** (role limited to
  ``compute.instances.stop``) using a short-lived token from the metadata
  service, so no static secret is written to disk.

These renderers are pure string construction so they can be unit-tested without
provisioning anything. See ``LESSONS.md`` Lesson 8 for why the VM must be able to
stop billing itself without depending on the orchestrator.
"""

import base64
from dataclasses import dataclass
from pathlib import Path

WATCHDOG_SCRIPT_PATH: Path = Path(__file__).resolve().parent / "idle_watchdog.sh"
WATCHDOG_REMOTE_PATH: str = "/opt/arf/idle_watchdog.sh"
WATCHDOG_BOOT_LOG: str = "/var/log/arf_idle_watchdog.boot.log"

# Production idle threshold; smoke tests override with a smaller value (e.g. 300).
DEFAULT_IDLE_THRESHOLD_SECONDS: int = 3600
DEFAULT_POLL_INTERVAL_SECONDS: int = 60
DEFAULT_IDLE_UTIL_PERCENT: int = 5
DEFAULT_GRACE_SECONDS: int = 600

# Vast.ai injects these per-instance env vars into every container (visible to the
# onstart script and processes it spawns). CONTAINER_API_KEY is scoped by Vast to
# this instance only — it can self-destroy but has no account-wide access — so the
# watchdog needs no manually-created API key. Confirmed by live test 2026-06-12.
VAST_SELF_ID_ENV: str = "CONTAINER_ID"
VAST_CONTAINER_API_KEY_ENV: str = "CONTAINER_API_KEY"

# TO CONFIRM against live Nebius docs before the first Nebius run (the smoke test
# runs on Vast, so this path is exercised only on first real Nebius use): the
# metadata endpoints that mint the instance service-account token and report the
# self instance id, and whether the base image ships the `nebius` CLI.
NEBIUS_METADATA_TOKEN_URL: str = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
)
NEBIUS_METADATA_INSTANCE_ID_URL: str = "http://169.254.169.254/computeMetadata/v1/instance/id"


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    idle_threshold_seconds: int = DEFAULT_IDLE_THRESHOLD_SECONDS
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    idle_util_percent: int = DEFAULT_IDLE_UTIL_PERCENT
    grace_seconds: int = DEFAULT_GRACE_SECONDS


def _encode_watchdog_script() -> str:
    return base64.b64encode(WATCHDOG_SCRIPT_PATH.read_bytes()).decode("ascii")


def _config_env_assignments(*, config: WatchdogConfig) -> str:
    # Space-separated VAR=value assignments consumed by idle_watchdog.sh.
    return (
        f"IDLE_THRESHOLD_SECONDS={config.idle_threshold_seconds} "
        f"POLL_INTERVAL_SECONDS={config.poll_interval_seconds} "
        f"IDLE_UTIL_PERCENT={config.idle_util_percent} "
        f"GRACE_SECONDS={config.grace_seconds}"
    )


def build_vast_terminate_cmd() -> str:
    # Runs via `bash -c` inside the watchdog. $CONTAINER_ID and $CONTAINER_API_KEY
    # are injected by Vast and inherited from the onstart env (no manual key).
    # `-y` is REQUIRED: `vastai destroy instance` prompts [y/N] and, with no TTY,
    # defaults to No and aborts, so the watchdog can never self-destroy without it.
    return (
        "pip install -q vastai >/dev/null 2>&1; "
        f'vastai destroy instance "${VAST_SELF_ID_ENV}" -y '
        f'--api-key "${VAST_CONTAINER_API_KEY_ENV}"'
    )


def build_nebius_terminate_cmd(*, profile_name: str) -> str:
    # Authenticates with the instance service-account token from the metadata
    # service (no static secret on disk) and stops *this* instance.
    return (
        f"TOKEN=\"$(curl -s -H 'Metadata-Flavor: Google' {NEBIUS_METADATA_TOKEN_URL} "
        '| python3 -c \'import sys,json;print(json.load(sys.stdin)["access_token"])\')"; '
        f"SELF_ID=\"$(curl -s -H 'Metadata-Flavor: Google' {NEBIUS_METADATA_INSTANCE_ID_URL})\"; "
        f'nebius -p {profile_name} --token "$TOKEN" compute instance stop --id "$SELF_ID"'
    )


def render_vast_onstart(*, config: WatchdogConfig) -> str:
    """Build the Vast.ai ``onstart`` script that installs and launches the watchdog.

    No API key is injected: the watchdog self-destroys with the Vast-provided
    ``CONTAINER_API_KEY`` env var, inherited from the onstart environment.
    """
    encoded: str = _encode_watchdog_script()
    terminate_cmd: str = build_vast_terminate_cmd()
    env_assignments: str = _config_env_assignments(config=config)
    return "\n".join(
        [
            "#!/bin/bash",
            "set -e",
            "mkdir -p /opt/arf",
            f"base64 -d > {WATCHDOG_REMOTE_PATH} <<'ARF_WD_B64'",
            encoded,
            "ARF_WD_B64",
            f"chmod +x {WATCHDOG_REMOTE_PATH}",
            f"{env_assignments} \\",
            f"  TERMINATE_CMD='{terminate_cmd}' \\",
            f"  nohup {WATCHDOG_REMOTE_PATH} >{WATCHDOG_BOOT_LOG} 2>&1 &",
            "",
        ]
    )


def render_nebius_cloud_init(
    *, ssh_public_key: str, profile_name: str, config: WatchdogConfig
) -> str:
    """Build a ``#cloud-config`` that authorizes SSH and installs the watchdog service."""
    encoded: str = _encode_watchdog_script()
    terminate_cmd: str = build_nebius_terminate_cmd(profile_name=profile_name)
    env_assignments: str = _config_env_assignments(config=config)
    # systemd unit body; TERMINATE_CMD is passed through the unit's Environment line.
    unit_lines: list[str] = [
        "[Unit]",
        "Description=ARF idle GPU dead-man's switch",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"Environment={env_assignments}",
        f"Environment=TERMINATE_CMD={terminate_cmd}",
        f"ExecStart=/bin/bash {WATCHDOG_REMOTE_PATH}",
        "Restart=on-failure",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    unit_body: str = "\n".join(unit_lines)
    cloud_config_lines: list[str] = [
        "#cloud-config",
        "ssh_authorized_keys:",
        f"  - {ssh_public_key}",
        "write_files:",
        f"  - path: {WATCHDOG_REMOTE_PATH}",
        "    encoding: b64",
        "    permissions: '0755'",
        f"    content: {encoded}",
        "  - path: /etc/systemd/system/arf-idle-watchdog.service",
        "    permissions: '0644'",
        "    content: |",
        *[f"      {line}" for line in unit_body.splitlines()],
        "runcmd:",
        "  - systemctl daemon-reload",
        "  - systemctl enable --now arf-idle-watchdog.service",
    ]
    return "\n".join(cloud_config_lines) + "\n"
