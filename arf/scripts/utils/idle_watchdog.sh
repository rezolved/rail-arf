#!/usr/bin/env bash
#
# idle_watchdog.sh — VM-side dead-man's switch for ARF GPU machines.
#
# Polls GPU utilization on a fixed interval. If EVERY GPU stays at or below the
# idle threshold continuously for IDLE_THRESHOLD_SECONDS, the watchdog invokes
# the provider-specific self-terminate command and exits. Any GPU showing
# activity above the threshold resets the idle timer to zero.
#
# This protects against the "nobody is driving the step" failure mode
# (rail-arf-serving LESSONS Lesson 8): the orchestrator's wakeup is
# fragile, so the VM must be able to stop billing itself without depending on
# Claude. A running benchmark keeps the GPU busy, so it never trips the timer;
# only a genuinely stranded VM is terminated.
#
# Designed to run from the provider's startup hook:
#   * vast.ai  — passed as the instance `onstart` script
#   * nebius   — installed via cloud-init as a systemd service
#
# Configuration is read from the environment so the same script body works on
# every provider; only TERMINATE_CMD differs (self-stop vs self-destroy).
#
#   IDLE_THRESHOLD_SECONDS  How long all GPUs must stay idle before terminating.
#                           Production: 3600 (60 min). Smoke test: 300 (5 min).
#                           Default: 3600.
#   POLL_INTERVAL_SECONDS   How often to sample nvidia-smi. Default: 60.
#   IDLE_UTIL_PERCENT       A GPU at or below this utilization counts as idle.
#                           Default: 5.
#   GRACE_SECONDS           Initial delay after boot before the watchdog arms,
#                           so engine load / model download is never mistaken
#                           for idle. Default: 600 (10 min).
#   TERMINATE_CMD           REQUIRED. The provider self-terminate command, run
#                           via `bash -c` when the idle threshold is reached
#                           (e.g. `vastai destroy instance "$CONTAINER_ID"` or
#                           `nebius compute instance stop --id <id>`).
#   WATCHDOG_LOG            Path to append diagnostics. Default:
#                           /var/log/arf_idle_watchdog.log.
#
set -euo pipefail

IDLE_THRESHOLD_SECONDS="${IDLE_THRESHOLD_SECONDS:-3600}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-60}"
IDLE_UTIL_PERCENT="${IDLE_UTIL_PERCENT:-5}"
GRACE_SECONDS="${GRACE_SECONDS:-600}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/var/log/arf_idle_watchdog.log}"

log() {
    printf '%s arf-idle-watchdog: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" \
        | tee -a "$WATCHDOG_LOG" >&2
}

if [ -z "${TERMINATE_CMD:-}" ]; then
    log "FATAL: TERMINATE_CMD is not set; refusing to run (a watchdog that cannot terminate is worse than none)."
    exit 2
fi

# Returns the maximum GPU utilization (integer percent) across all GPUs.
# Treats an nvidia-smi failure or empty output as 0 (idle): a box whose GPUs
# cannot be queried is not doing useful work and should count toward shutdown.
max_gpu_util() {
    local out
    if ! out="$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null)"; then
        echo 0
        return 0
    fi
    local max=0
    local val
    while IFS= read -r val; do
        val="${val// /}"
        [ -z "$val" ] && continue
        # Guard against non-numeric lines (e.g. "[Not Supported]").
        case "$val" in
            ''|*[!0-9]*) continue ;;
        esac
        if [ "$val" -gt "$max" ]; then
            max="$val"
        fi
    done <<< "$out"
    echo "$max"
}

log "starting: threshold=${IDLE_THRESHOLD_SECONDS}s poll=${POLL_INTERVAL_SECONDS}s idle_util<=${IDLE_UTIL_PERCENT}% grace=${GRACE_SECONDS}s"
sleep "$GRACE_SECONDS"
log "armed after grace period"

idle_accum=0
while true; do
    util="$(max_gpu_util)"
    if [ "$util" -le "$IDLE_UTIL_PERCENT" ]; then
        idle_accum=$((idle_accum + POLL_INTERVAL_SECONDS))
        log "idle: max_util=${util}% accumulated_idle=${idle_accum}s/${IDLE_THRESHOLD_SECONDS}s"
    else
        if [ "$idle_accum" -ne 0 ]; then
            log "activity: max_util=${util}% — resetting idle timer"
        fi
        idle_accum=0
    fi

    if [ "$idle_accum" -ge "$IDLE_THRESHOLD_SECONDS" ]; then
        log "IDLE THRESHOLD REACHED (${idle_accum}s >= ${IDLE_THRESHOLD_SECONDS}s) — self-terminating now"
        log "running TERMINATE_CMD: ${TERMINATE_CMD}"
        bash -c "$TERMINATE_CMD" >>"$WATCHDOG_LOG" 2>&1 || \
            log "WARNING: TERMINATE_CMD exited non-zero; will retry next poll"
        # Do not exit on terminate failure: keep retrying so a transient API
        # error cannot leave a stranded VM billing indefinitely.
    fi

    sleep "$POLL_INTERVAL_SECONDS"
done
