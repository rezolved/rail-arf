---
name: "setup-remote-machine"
description: >-
  Provision, validate, monitor, and tear down remote GPU machines for task
  execution. Use when a task needs remote machine setup, monitoring, or
  teardown.
---
# Setup Remote Machine

**Version**: 9

## Goal

Acquire a 2×H100 Azure ML compute instance from the shared pool defined in `project/azure_vm.json`,
verify SSH connectivity and GPU availability, prepare the execution environment, and make the
machine ready for task execution. Also defines the teardown protocol and execution monitoring
procedures.

## Inputs

* `$TASK_ID` — the task folder name (e.g., `t0004_baseline_replication`)

## Context

Read before starting:

* `tasks/$TASK_ID/plan/plan.md` — Section 5 (Remote Machines) defines GPU requirements, estimated
  runtime, and budget
* `arf/specifications/remote_machines_specification.md` — `machine_log.json` schema, lifecycle
  states, cost protocol
* `arf/specifications/task_results_specification.md` — schemas for `remote_machines_used.json` and
  `costs.json`
* `project/azure_vm.json` — Azure ML compute pool (primary, fallbacks, hourly cost)
* `project/budget.json` — per-task budget limit
* `arf/scripts/utils/azure_ml_vm.py` — Python library and CLI for acquire/run/teardown against the
  Azure ML pool. The library exposes `acquire()`, `run()`, `teardown()`, and
  `to_machine_log_entry()` for schema-compatible `machine_log.json` shaping.
* Current project spend and budget left — run:
  ```bash
  uv run python -u -m arf.scripts.aggregators.aggregate_costs --format json --detail full
  ```

* * *

## Critical Rules

1. Wrap ALL CLI commands (`az`, `ssh`, `python -m arf.scripts.utils.azure_ml_vm`) with
   `run_with_logs.py`:

   ```bash
   uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
     uv run python -m arf.scripts.utils.azure_ml_vm acquire $TASK_ID
   ```

   The examples below show raw commands for clarity; always add the `run_with_logs.py` wrapper when
   executing them.

2. The pool is shared with the finetuning team. Coordinate via the `#finetuning` Slack channel
   before scheduling long-running jobs. Both pool VMs (`FT-NC80-v3`, `FT-NC80-v2`) carry their own
   Azure-side 60-min idle shutdown as a safety net.

3. Auto-deallocate after teardown. At $13.96/hr per VM, leaving an instance running overnight costs
   ~$110. `azure_ml_vm teardown` will stop the VM unless another task holds a lock on it; only pass
   `--keep-running` when the user has explicitly approved keeping the VM hot.

4. **Keep the step heart-beating.** Provisioning routinely runs 10+ minutes — cold start alone is ~4
   minutes, before SSH setup, storage checks, and version capture. Refresh the step tracker after
   each phase:

   ```bash
   uv run python -m arf.scripts.utils.heartbeat write $TASK_ID <step_number> \
     setup-remote-machine/setup-machines
   ```

   This is the step-tracker heartbeat, which is a different thing from the `heartbeat_path` file on
   the VM recorded for jobs over 2 hours (Phase 4). The tracker heartbeat is what tells the
   orchestrator this step is still alive; the VM file tracks a long job's progress. A provisioning
   step that stops beating is indistinguishable from one whose executor died — and it dies holding a
   billing GPU, which is the worst step in the whole workflow to go silent in.

* * *

## Steps

### Phase 1: Pre-flight checks

1. Read `tasks/$TASK_ID/plan/plan.md` Section 5 (Remote Machines). Confirm the plan targets a 2×H100
   / NC80-class machine. The pool only contains that SKU; non-NC80 plans need to be re-cut before
   this skill runs.

2. Check current project spend and budget left:

   ```bash
   uv run python -u -m arf.scripts.aggregators.aggregate_costs --format json --detail full
   ```

   * If `stop_threshold_reached` is true or the plan's estimated GPU spend exceeds
     `budget_left_before_stop_usd`, create `tasks/$TASK_ID/intervention/budget_exceeded.md` and
     STOP.
   * Estimated GPU spend: `estimated_hours * 13.96` USD per VM.

3. Verify `az` is authenticated:

   ```bash
   az account show -o json
   ```

   If this fails or returns a different subscription, stop and ask the user to run `az login` /
   `az account set`.

### Phase 2: Acquire a VM from the pool

1. Call the provisioner. It iterates pool entries in priority order, starts a stopped VM if needed,
   waits up to 8 minutes for SSH, and places `~/.arf-locks/$TASK_ID.lock` on the VM:

   ```bash
   uv run python -m arf.scripts.utils.azure_ml_vm acquire $TASK_ID
   ```

   * Exit code `0` — success; JSON on stdout includes `vm_name`, `ssh_host_alias`,
     `hourly_cost_usd`, `acquired_at`, `started_vm`, and `failed_attempts`.
   * Exit code `75` — pool busy; the provisioner has written
     `tasks/$TASK_ID/intervention/pool_busy.md`. STOP and let the user resolve.
   * Exit code `1` — generic error; surface stderr to the user and STOP.

   Between the SSH check and the lock, `acquire` runs `arf/scripts/utils/remote_preflight.sh` on the
   box. It enables `systemd` lingering for the SSH user and guarantees `/mnt/cache/persist` resolves
   to the real Azure Files mount, repairing the symlink when it points at the ephemeral disk. These
   are LESSONS.md Lessons 11 and 10 — a killed training job and a lost adapter — and both used to be
   prose in this skill that nothing executed. A VM that fails preflight is **not used**: the attempt
   is recorded in `failed_attempts` with `failure_phase: "preflight"` and the provisioner moves to
   the next pool entry. If every entry fails that way, read the recorded reasons before touching a
   box by hand; a whole pool failing preflight usually means the image or the mount changed, not
   that four VMs broke at once.

2. Save the JSON output as the basis for `machine_log.json`. Use `to_machine_log_entry()` from the
   library to convert it to the schema consumed by `aggregate_machines.py`:

   ```python
   from arf.scripts.utils.azure_ml_vm import to_machine_log_entry
   ```

   Write the result to `tasks/$TASK_ID/logs/steps/NNN_setup-machines/machine_log.json` as a
   one-element JSON array. Required fields populated at acquire time include `provider`,
   `instance_id`, `selected_offer`, `created_at`, `ready_at`, `search_started_at`,
   `total_provisioning_seconds`, and `failed_attempts`. `destroyed_at` and `total_cost_usd` remain
   `null` until teardown.

### Phase 3: Verify GPU and CUDA

1. Verify GPU availability over SSH (host alias resolves via the user's `~/.ssh/config`):

   ```bash
   ssh FT-NC80-v3 "nvidia-smi --query-gpu=name,memory.total,driver_version \
     --format=csv,noheader"
   ```

   Expect two H100 80GB lines. If only one GPU is visible, destroy the lock and try the fallback.

2. Verify CUDA toolkit:

   ```bash
   ssh FT-NC80-v3 "nvcc --version"
   ```

3. Update `machine_log.json` with `gpu_verified` and `cuda_version`.

4. **Install the idle watchdog** (MANDATORY — no step may run work on a machine without one). For
   Vast.ai and Nebius the watchdog is baked into the creation hook (`onstart` / `#cloud-config`) and
   is already running by this phase, so confirm it rather than reinstalling. Azure ML pool VMs are
   acquired already running and have no creation hook, so install over SSH:

   ```bash
   uv run python -u -c "
   from arf.scripts.utils.watchdog_provisioning import (
       WatchdogConfig, render_azure_ml_install_script)
   print(render_azure_ml_install_script(
       vm_name='$VM_NAME', resource_group='$RESOURCE_GROUP',
       workspace_name='$WORKSPACE_NAME', config=WatchdogConfig()))
   " | ssh "$SSH_HOST" "bash -s"
   ```

   The install is idempotent — safe to re-run on a pool VM that already carries a watchdog.

5. Confirm the watchdog is actually alive before recording success:

   ```bash
   ssh "$SSH_HOST" "pgrep -f idle_watchdog.sh"
   ```

   Record `watchdog_active: true` and `watchdog_idle_timeout_seconds` in `machine_log.json` only
   when a PID comes back. On failure record `watchdog_active: false` and treat it as a blocker: fix
   the install or tear the machine down. Never record `true` without a confirmed PID — a watchdog
   that cannot terminate protects nothing while making the machine look protected.

   The watchdog is what bounds spend when the orchestrator side fails. A ghosted step, a session
   that ended without a `ScheduleWakeup`, and a missed resume all end the same way: a machine nobody
   is driving. Only the machine itself can stop billing then. `verify_step_liveness` flags a live
   machine without `watchdog_active: true` as `ST-W008`.

### Phase 4: Prepare environment

1. Copy data to the remote VM. Use `scp` for files under 100 MB; for larger transfers use `rsync`:

   ```bash
   scp $LOCAL_PATH FT-NC80-v3:$REMOTE_PATH
   rsync -av $LOCAL_DIR/ FT-NC80-v3:$REMOTE_DIR/
   ```

2. Install task-specific dependencies (vLLM, model checkpoints, etc.) inside the project conda env
   on the VM. The pool VMs ship with conda + CUDA already installed by the Azure ML image.

3. Run a GPU smoke test:

   ```bash
   ssh FT-NC80-v3 "python -c 'import torch; print(torch.cuda.device_count())'"
   ```

   Expect the number of GPUs declared in the pool entry (e.g., `2` for an `NC80` H100 VM).

4. **Engine smoke gate** (MANDATORY for any task that will issue measurement requests). After
   installing the task-specific engine (vLLM, SGLang, TRT-LLM, etc.) and launching it, issue one
   trivial request (a `health`/`/version` endpoint check **and** one minimum-length chat completion)
   before any warmup or measured phase. If either fails, mark the condition `null` and skip directly
   to teardown — do not waste VM time on a broken engine. Record the smoke result in
   `machine_log.json` under `smoke_gate_status` (`pass` or `fail`) with the failure reason. See
   `LESSONS.md` (Lesson 2: smoke-gate before measurement) for the rationale.

5. For jobs over 2 hours, configure checkpointing and a heartbeat file. Record both paths in
   `machine_log.json` as `checkpoint_path` and `heartbeat_path`. See
   `arf/specifications/remote_machines_specification.md` "Mandatory Checkpointing".

* * *

## Execution on Remote VMs

Referenced by the `implementation` step in execute-task.

### Running long jobs

Lingering and the persistent-storage symlink are **already guaranteed** by the time execution
starts: `azure_ml_vm acquire` runs `arf/scripts/utils/remote_preflight.sh` on the box before it
places the lock, and refuses the VM when either guarantee fails (Phase 2, step 1). Nothing here
needs to re-run them. What follows is why they matter, so a `preflight` failure in `failed_attempts`
is recognizable.

`tmux` alone does NOT survive SSH disconnection (LESSONS.md Lesson 11): on systemd-managed hosts
`systemd-logind` tears down the whole login-session cgroup scope — killing everything inside `tmux`,
SIGTERM then SIGKILL — as soon as the launching SSH session ends, unless lingering is enabled for
that user. The failure is silent: `tmux has-session` still reports the session alive for a window
after disconnect, and the job log shows no error before the kill.

`/mnt/cache/persist` must resolve to the Azure Files share, not the ephemeral `/mnt` temp disk
(LESSONS.md Lesson 10). A `mkdir -p` under the ephemeral disk succeeds silently and the data is
wiped on the next VM stop. To re-check either guarantee by hand:

```bash
ssh FT-NC80-v3 "loginctl show-user azureuser | grep Linger"   # must show Linger=yes
ssh FT-NC80-v3 "readlink -f /mnt/cache/persist && df -h /mnt/cache/persist"
```

Launch long jobs inside `tmux` so they survive SSH disconnection:

```bash
ssh FT-NC80-v3 "tmux new-session -d -s work \
  'python train.py > /home/azureuser/output.log 2>&1; echo DONE >> /home/azureuser/output.log'"
```

For jobs the orchestrator wants to monitor synchronously, use the library `run` entry point — it
emits heartbeats every 5 min and checkpoint reminders every 30 min for jobs marked >2h:

```bash
uv run python -m arf.scripts.utils.azure_ml_vm run $TASK_ID -- \
  tmux new-session -d -s work 'python train.py'
```

### Monitoring

* Check the tmux session:
  ```bash
  ssh FT-NC80-v3 "tmux has-session -t work && echo running || echo finished"
  ```
* Tail logs:
  ```bash
  ssh FT-NC80-v3 "tail -50 /home/azureuser/output.log"
  ```
* Confirm the VM is still up:
  ```bash
  az ml compute show --name FT-NC80-v3 \
    --workspace-name finetuning-workspace --resource-group rezolve-AI -o json
  ```

### Handling disconnection

If the SSH session drops:

1. Verify the VM is still running via `az ml compute show`.
2. Reconnect: `ssh FT-NC80-v3 "tmux ls"`.
3. Tail the log to confirm progress.
4. The job continues inside tmux regardless of SSH disconnection.

* * *

## Teardown Protocol

Executed during the `teardown` step of execute-task.

### Steps

1. Confirm the job has finished:

   ```bash
   ssh FT-NC80-v3 "tmux has-session -t work && echo STILL_RUNNING || echo DONE"
   ```

   If still running, wait or investigate. NEVER tear down with a live job unless the user explicitly
   instructs it.

2. Download results from the VM:

   ```bash
   rsync -av FT-NC80-v3:$REMOTE_RESULTS_DIR/ tasks/$TASK_ID/$LOCAL_DEST/
   ```

3. Release the lock and (if no other task holds a lock on the VM) deallocate the VM:

   ```bash
   uv run python -m arf.scripts.utils.azure_ml_vm teardown $TASK_ID \
     --acquired-at $CREATED_AT
   ```

   `--acquired-at` should be the `acquired_at` value from the Phase 2 acquire output; it lets the
   provisioner compute `total_duration_hours` and `total_cost_usd`. The output JSON reports
   `deallocated` (true if `az ml compute stop` ran) and `other_locks_present` (true if a sibling
   task held a lock and the VM was left running).

4. Update `machine_log.json`. Use `to_machine_log_entry(acquire_result=..., teardown_result=...)` to
   refresh the entry with `destroyed_at`, `total_duration_hours`, and `total_cost_usd`.

5. Update results files. Use exactly the field names shown — do not use aliases from
   `machine_log.json`.

   `results/remote_machines_used.json`:

   ```json
   [
     {
       "provider": "azure-ml",
       "machine_id": "FT-NC80-v3",
       "gpu": "2xH100",
       "gpu_count": 2,
       "ram_gb": 880,
       "duration_hours": 2.50,
       "cost_usd": 34.90
     }
   ]
   ```

   `results/costs.json` — add the machine cost to `breakdown` using key `"azure-ml-2xh100"`:

   ```json
   {
     "total_cost_usd": 34.90,
     "breakdown": {
       "azure-ml-2xh100": 34.90
     }
   }
   ```

6. Run the machine destruction verificator:

   ```bash
   uv run python -m arf.scripts.verificators.verify_machines_destroyed --task-id $TASK_ID
   ```

   Fix any errors before proceeding.

* * *

## Done When

* `machine_log.json` exists with all required fields populated: `provider="azure-ml"`, `instance_id`
  (VM name), `selected_offer`, `created_at`, `ready_at`, `search_started_at`,
  `total_provisioning_seconds`, `failed_attempts`, plus `destroyed_at` and `total_cost_usd` after
  teardown.
* SSH connectivity verified and `nvidia-smi` output logged (two H100 80GB lines).
* All data and scripts copied to the VM.
* Smoke test (`torch.cuda.device_count() == 2`) passes.
* For jobs >2h: `checkpoint_path` and `heartbeat_path` set in `machine_log.json`.
* Step log written with VM acquired, SSH details, and any `failed_attempts` summary.

For teardown:

* All results downloaded and verified locally.
* `azure_ml_vm teardown` exited `0`; output JSON inspected — `deallocated=true` unless another task
  legitimately holds a lock on the same VM.
* `machine_log.json` has `destroyed_at` and `total_cost_usd`.
* `remote_machines_used.json` and `costs.json` updated.
* `verify_machines_destroyed.py` passes with no errors.

* * *

## Forbidden

* NEVER run `prestep` or `poststep` — the orchestrator handles the step lifecycle.
* NEVER commit — the orchestrator handles all commits.
* NEVER modify `step_tracker.json` directly — the orchestrator manages step state. Refreshing the
  heartbeat via `arf.scripts.utils.heartbeat` (Critical Rule 4) is the one sanctioned exception: it
  touches only `last_heartbeat_at` and `current_owner`, never status or step structure.
* NEVER write `step_log.md` — the orchestrator writes it after this skill completes.
* NEVER leave a VM running without a corresponding `teardown` step in `step_tracker.json`.
* NEVER skip the budget check in Phase 1.
* NEVER tear down a VM while a job is still running (unless the user explicitly instructs it).
* NEVER pass `--keep-running` to `azure_ml_vm teardown` without explicit user approval — the default
  auto-stop is the primary defense against forgotten $13.96/hr instances.
* NEVER commit Azure credentials or SSH private keys. The SSH key path lives in the user's
  `~/.ssh/config` entry for `FT-NC80-v3` / `FT-NC80-v2`, not in the repo.
* NEVER edit `project/azure_vm.json` to point at a personal VM without coordinating with the
  finetuning team — the pool is shared infrastructure.
