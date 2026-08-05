#!/usr/bin/env bash
#
# remote_preflight.sh — VM-side guarantees that must hold before any long job runs.
#
# Two failures cost this project real work before they were understood, and both
# are silent: nothing errors, the job just dies or the data is gone later.
#
#   * LESSONS Lesson 11 — `tmux` alone does NOT survive SSH disconnection. On a
#     systemd host, `systemd-logind` tears down the whole login-session cgroup
#     scope (SIGTERM then SIGKILL to everything inside `tmux`) when the launching
#     SSH session ends, unless lingering is enabled for that user. A DPO run died
#     at step 27/540 this way, with a clean log right up to the kill.
#   * LESSONS Lesson 10 — `/mnt/cache/persist` must resolve to the Azure Files
#     share. `/mnt` itself is an ephemeral temp disk wiped on every VM stop, and
#     `mkdir -p` on it succeeds happily. An SFT-LoRA adapter was lost this way and
#     survived only because it had been DVC-pushed first.
#
# Both used to be prose in a skill that nothing executed. This script is run by
# `azure_ml_vm.acquire()` over SSH before it places the task lock: a VM that fails
# here is not used at all, and the provisioner moves to the next pool entry.
#
# Configuration (both exist so the script is testable in a sandbox; the defaults
# are what runs in production):
#
#   ARF_PERSIST_LINK        The persistent-storage symlink to guarantee.
#                           Default: /mnt/cache/persist
#   ARF_PERSIST_MOUNT_ROOT  What that link must resolve to.
#                           Default: the Azure Files mount for this host.
#
# On success: exit 0, printing exactly one line of JSON on stdout. On failure:
# non-zero, with the failing guarantee named on stderr and stdout left empty —
# the caller parses stdout, so a partial object there would be worse than silence.
#
# Deliberately POSIX-ish (no `pipefail`, no arrays): it is shipped as the argv of
# an `ssh` command and interpreted by whatever login shell the image ships.
#
set -eu

PERSIST_LINK="${ARF_PERSIST_LINK:-/mnt/cache/persist}"
PERSIST_MOUNT_ROOT="${ARF_PERSIST_MOUNT_ROOT:-/mnt/batch/tasks/shared/LS_root/mounts/clusters/$(hostname)/code}"

fail() {
    printf 'arf-preflight: %s\n' "$1" >&2
    exit 1
}

USER_NAME="$(whoami 2>/dev/null || id -un 2>/dev/null || true)"
if [ -z "$USER_NAME" ]; then
    fail "cannot determine the SSH user name (whoami and id -un both failed)"
fi

# --- Guarantee 1: systemd lingering (Lesson 11) ------------------------------
# Enabling is best-effort (unprivileged first, then sudo); the *verification* is
# what counts. An enable-linger that silently no-ops leaves a box that looks
# prepared and kills the training job on disconnect anyway.
if ! command -v loginctl >/dev/null 2>&1; then
    fail "loginctl not found; this host is not systemd-managed, so tmux survival across SSH disconnect cannot be guaranteed (LESSONS Lesson 11)"
fi

# `sudo -n`: never prompt. ssh forwards the local stdin, so a prompting sudo could
# hang until the caller's timeout instead of failing fast.
loginctl enable-linger "$USER_NAME" >/dev/null 2>&1 ||
    sudo -n loginctl enable-linger "$USER_NAME" >/dev/null 2>&1 ||
    true

LINGER_STATE="$(loginctl show-user "$USER_NAME" 2>/dev/null | grep '^Linger=' | head -1 | cut -d= -f2 || true)"
if [ "$LINGER_STATE" != "yes" ]; then
    fail "linger is not enabled for ${USER_NAME} (Linger=${LINGER_STATE:-unknown}); a detached tmux job would be killed when the SSH session ends (LESSONS Lesson 11)"
fi

# --- Guarantee 2: persistent storage (Lesson 10) -----------------------------
if [ ! -d "$PERSIST_MOUNT_ROOT" ]; then
    fail "persist mount root ${PERSIST_MOUNT_ROOT} does not exist; the Azure Files share is not mounted, so anything written under ${PERSIST_LINK} would land on the ephemeral disk and vanish on the next VM stop (LESSONS Lesson 10)"
fi

# A real directory where the link belongs is not something to silently replace:
# `ln -sfn` would drop a stray symlink *inside* it and the mistake would compound.
if [ -d "$PERSIST_LINK" ] && [ ! -L "$PERSIST_LINK" ]; then
    fail "persist path ${PERSIST_LINK} is a real directory, not a symlink to ${PERSIST_MOUNT_ROOT}; it may already hold data written to the ephemeral disk — inspect it by hand rather than letting this script replace it"
fi

EXPECTED_TARGET="$(readlink -f "$PERSIST_MOUNT_ROOT")"
RESOLVED_TARGET="$(readlink -f "$PERSIST_LINK" 2>/dev/null || true)"

if [ "$RESOLVED_TARGET" != "$EXPECTED_TARGET" ]; then
    # The link lives under the ephemeral disk, which is wiped on every VM stop, so
    # its parent directory is routinely missing on a restarted pool VM. `ln` does not
    # create intermediate directories; without this, every restarted VM in the pool
    # would fail preflight and be skipped.
    mkdir -p "$(dirname "$PERSIST_LINK")" 2>/dev/null || true
    ln -sfn "$PERSIST_MOUNT_ROOT" "$PERSIST_LINK" ||
        fail "persist link ${PERSIST_LINK} points at ${RESOLVED_TARGET:-nothing} and could not be repaired to ${PERSIST_MOUNT_ROOT}"
    RESOLVED_TARGET="$(readlink -f "$PERSIST_LINK" 2>/dev/null || true)"
    if [ "$RESOLVED_TARGET" != "$EXPECTED_TARGET" ]; then
        fail "persist link ${PERSIST_LINK} still resolves to ${RESOLVED_TARGET:-nothing} after repair, not ${EXPECTED_TARGET}"
    fi
fi

# The caller parses the JSON line below and drops anything unparseable, so a path
# carrying a quote or backslash would reject a healthy VM with no diagnostic anywhere.
# Refuse loudly instead of emitting JSON that cannot be read.
case "$RESOLVED_TARGET" in
    *'"'*|*'\'*)
        fail "persist path ${RESOLVED_TARGET} contains a quote or backslash and cannot be reported as JSON"
        ;;
esac

# Resolving correctly is not the same as being writable: an unwritable share fails at
# the first checkpoint, hours into a run.
WRITE_PROBE="${PERSIST_LINK}/.arf_preflight_write_test.$$"
if ! touch "$WRITE_PROBE" 2>/dev/null; then
    fail "persist path ${RESOLVED_TARGET} is not writable by ${USER_NAME}; checkpoints would fail mid-run"
fi
rm -f "$WRITE_PROBE" 2>/dev/null || true

printf '{"linger_enabled": true, "persist_path": "%s", "persist_writable": true}\n' "$RESOLVED_TARGET"
