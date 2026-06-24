---
name: "execute-task"
description: "Run an ARF task through all required stages and merge the final PR."
---
# Execute Task

**Version**: 25

## Goal

Execute a complete task through all mandatory stages and finish with a merged PR and refreshed
`overview/` on `main`.

## Inputs

* `$TASK_ID` — the task folder name (e.g., `t0003_download_semcor_dataset`)

* * *

## Part A — Coordinator

The coordinator is a thin orchestrator. It reads three files, spawns a fresh step-executor Agent for
each pending step, and handles only Phase −1 (liveness) and Phases 7-9 (PR/merge/overview) inline.

## Coordinator Context

Read before starting **and at every wakeup**:

* `tasks/$TASK_ID/task.json` — task objective and dependencies
* `tasks/$TASK_ID/step_tracker.json` — current step state (may not exist on first run)
* `tasks/$TASK_ID/checkpoint.md` — accumulated step decisions (may not exist on first run)

Do NOT read specification files, plan files, research files, or step logs. Those are loaded by
step-executors only.

## Phase −1: Wakeup begins with a liveness scan

This phase runs at the very start of every invocation of `execute-task`, including the first one on
a fresh task, every resume from `/loop` or `ScheduleWakeup`, and every continuation after a subagent
handoff. It must execute before any other phase.

1. Run the liveness verificator:

   ```bash
   uv run python -m arf.scripts.verificators.verify_step_liveness --all
   ```

   Exit code `0` means no stuck step was detected — proceed to Phase 0.

2. If the verificator returns non-zero, a step needs recovery before any new work. Two error codes
   can appear:

   * `ST-E007` — an `in_progress` step has a stale heartbeat AND a live VM is still provisioned: the
     idle-billing emergency.
   * `ST-E008` — a `paused_waiting` step has `watchdog_active != true`: an unsafe pause. Either
     confirm/install the idle watchdog and set `watchdog_active`, or drive the step synchronously /
     transition it to `blocked_intervention`. Never leave a step paused without a watchdog.

   Do not continue with Phase 0. Instead:

   * Identify the offending `task_id` and `step_number` from the verificator output.
   * Invoke `/diagnose-stuck-step` inline (do NOT delegate to a fresh subagent — the coordinator
     owns the recovery) with that `task_id` and `step_number`. The skill produces a structured
     report at `tasks/<task_id>/logs/diagnostics/<timestamp>_<step>.json` and prints its path.
   * Read the diagnostic report. Act on its `recommended_action`:
     * `resume_inline` — drive the stuck step's work inline (no new subagent), then mark it
       completed via `arf.scripts.utils.heartbeat.complete_step`.
     * `teardown_and_restart` — invoke the teardown step from `setup-remote-machine`, then either
       restart the work inline or transition the step to `blocked_intervention` with a written
       intervention file describing the failure and what the human should look at.
     * `intervention_required` — transition the step to `blocked_intervention` with the diagnostic
       report path referenced.

3. Warnings only (`ST-W005` or `ST-W006` without `ST-E007`): there is no live VM burning money, but
   a step is ghosted or pathologically slow. Run `/diagnose-stuck-step` inline as above, then act on
   the report. The cost pressure is lower but the coordinator still owns the recovery.

4. After acting on every flagged step, re-run `verify_step_liveness --all`. Only when it returns `0`
   may the coordinator proceed to Phase 0.

5. Resume paused steps. Scan every `step_tracker.json` for steps with status `paused_waiting`. These
   pass the liveness verificator (their VM is watchdog-protected), so they do **not** appear in step
   1's output — the coordinator must find them explicitly. For each, in task order:

   * If `now < resume_after`: the wait is not over. Register one `ScheduleWakeup` for the earliest
     `resume_after` across all paused steps and STOP this invocation — do not start Phase 0 work.
     The VM's idle watchdog protects spend in the meantime.
   * If `now >= resume_after`: re-dispatch the step's skill (e.g. `/implementation`) in resume mode
     — spawn its subagent, passing the step's `resume_sentinel` and instructions to re-check it. The
     skill then either drives the step to a terminal state or calls `pause_step` again with a new
     `resume_after`.

   Only when no `paused_waiting` step remains pending resume may the coordinator proceed to Phase 0.

Critical rule: NEVER re-delegate **recovery** (ghosted/emergency steps from steps 2-3) to a fresh
subagent — drive it inline (the failure mode behind the 9-hour idle incident, `LESSONS.md` Lesson
9). This is distinct from the sanctioned **resume** path in step 5: a `paused_waiting` step on a
watchdog-protected VM may legitimately end the session and resume on a scheduled wakeup, because the
watchdog — not the coordinator — is what guarantees the VM cannot run away.

## Coordinator Loop

### Phase 0: Bootstrap

1. Read `tasks/$TASK_ID/task.json` using the Read tool — note the objective, dependencies, and
   `task_types`.
2. Read `tasks/$TASK_ID/step_tracker.json` if it exists — find the next pending step. If it does not
   exist, skip to the step loop: the first step-executor (`create-branch`) will create it.
3. Read `tasks/$TASK_ID/checkpoint.md` if it exists.
   * If `step_tracker.json` has completed steps but `checkpoint.md` is absent, this is a pre-v25
     task. Synthesize a minimal `checkpoint.md` before spawning the next step-executor — see
     **Resume of Pre-v25 Tasks** below.

### Step Loop

For each step with status `"pending"` in `step_tracker.json`, in step order:

1. Determine `step_id` and `step_number` from `step_tracker.json`.
2. Spawn a step-executor Agent using the **Step-Executor Prompt Template**, substituting `$TASK_ID`,
   `$STEP_ID`, `$STEP_NUMBER`, `$SHORT_DESCRIPTION`, and the full contents of
   `tasks/$TASK_ID/checkpoint.md`.
3. Wait for the step-executor to return `{"status": "...", "notes": "..."}`.
4. On `"completed"`: re-read `step_tracker.json` to confirm the step is marked `completed`. If
   `step_id` was `create-branch`, also create `tasks/$TASK_ID/checkpoint.md` — see **Coordinator
   Creates Checkpoint After Step 1** below. Proceed to next pending step.
5. On `"failed"`: report to user with the `notes` field and stop.
6. On `"paused"`: verify `step_tracker.json` shows `paused_waiting` with `watchdog_active: true`.
   Register `ScheduleWakeup` for `resume_after`. Stop this invocation.

### Phases 7-9: Inline

After all steps complete (all `step_tracker.json` entries are `completed` or `skipped`), execute
Phases 7, 8, and 9 inline in the coordinator. Do not spawn step-executors for these phases. See
Phase 7, 8, and 9 instructions in Part B.

## Coordinator Creates Checkpoint After Step 1

After `create-branch` (step 1) completes, the coordinator writes `tasks/$TASK_ID/checkpoint.md`:

```yaml
---
spec_version: "1"
task_id: "$TASK_ID"
updated_at: "<ISO8601 UTC>"
completed_steps: 1
next_step_number: 2
next_step_id: "<step-2-id from step_tracker.json>"
---
```

```markdown
# Task Objective
<short_description from task.json>

---

## Step History

### Step 1 — create-branch
Branch `task/$TASK_ID` created. Initial folder structure initialized in `tasks/$TASK_ID/`.
Step 1 is a mechanical setup step with no research output.

---

## Cross-Step Decisions

---

## Next Step Notes
Step 1 completed successfully. The task branch and folder are ready. Proceed to step 2 per
step_tracker.json.
```

Run `uv run flowmark --inplace --nobackup tasks/$TASK_ID/checkpoint.md`. Commit with message:
`$TASK_ID [coordinator]: Initialize checkpoint.md after step 1`

## Resume of Pre-v25 Tasks

When `step_tracker.json` has completed steps but `checkpoint.md` is absent:

1. Read `step_tracker.json` and `tasks/$TASK_ID/task.json`.
2. Write `tasks/$TASK_ID/checkpoint.md` with:
   * Frontmatter: `spec_version: "1"`, `task_id: "$TASK_ID"`, `updated_at: <now>`,
     `completed_steps: <count of completed+skipped steps>`,
     `next_step_number: <next pending step number>`, `next_step_id: <next pending step id>`.
   * `# Task Objective`: `short_description` from `task.json`.
   * `## Step History`: one `### Step N — <step-id>` entry per completed or skipped step, body:
     `"Synthesized for v25 resume — no detail available."`.
   * `## Cross-Step Decisions`: empty.
   * `## Next Step Notes`:
     `"Resuming pre-v25 task. Prior step details unavailable; read plan.md and step logs for context."`
3. Run `uv run flowmark --inplace --nobackup tasks/$TASK_ID/checkpoint.md`.
4. Commit: `$TASK_ID [coordinator]: Synthesize checkpoint.md for v25 resume`

## Step-Executor Prompt Template

Use this exact template when spawning step-executors. Fill in all placeholders.

```text
You are a step-executor for ARF task $TASK_ID.

Your assignment: execute step $STEP_NUMBER — "$STEP_ID"

Task objective: $SHORT_DESCRIPTION

Accumulated context (checkpoint.md):
---
$CHECKPOINT_MD_CONTENTS
---

Instructions:
1. Read arf/skills/execute-task/SKILL.md — find "Part B — Step-Executor" and follow the protocol
   for the "$STEP_ID" step.
2. Load only the specs listed for "$STEP_ID" in the Per-Step Spec Table in that same file. Do not
   read other specs.
3. Complete the step, update checkpoint.md if step_order >= 2, commit all work, run poststep.
4. Return exactly one of:
   {"status": "completed", "notes": "<one sentence summary>"}
   {"status": "failed", "notes": "<failure reason>"}
   {"status": "paused", "notes": "<resume sentinel and ETA>"}
```

## Per-Step Spec Table

Each step-executor reads only the specs listed here. No other spec files.

| Step ID | Specs to read |
| --- | --- |
| `create-branch` | `task_steps_specification.md`, `task_git_specification.md`, `task_file_specification.md`, `logs_specification.md`, `project_budget_specification.md` |
| `check-deps` | `logs_specification.md` |
| `init-folders` | `task_file_specification.md`, `logs_specification.md` |
| `research-papers` | `research_papers_specification.md`, `logs_specification.md` |
| `research-internet` | `research_internet_specification.md`, `logs_specification.md` |
| `research-code` | `research_code_specification.md`, `logs_specification.md` |
| `planning` | `plan_specification.md`, `project_budget_specification.md`, `logs_specification.md` |
| `setup-machines` | `remote_machines_specification.md`, `logs_specification.md` |
| `implementation` | `plan/plan.md`, asset specs for each expected asset type in `task.json`, `logs_specification.md` |
| `teardown` | `remote_machines_specification.md`, `logs_specification.md` |
| `creative-thinking` | `logs_specification.md` |
| `results` | `task_results_specification.md`, `logs_specification.md` |
| `compare-literature` | `compare_literature_specification.md`, `logs_specification.md` |
| `suggestions` | `logs_specification.md` |
| `reporting` | `task_results_specification.md`, `logs_specification.md` |

* * *

## Part B — Step-Executor

A step-executor is a fresh Agent spawned for exactly one step. Read only the files listed for your
step in the Per-Step Spec Table and the `checkpoint.md` contents passed in your prompt. Do not load
other spec files, plan files from other tasks, or research files outside your step's scope.

## Step-Executor Protocol

Every step follows this exact sequence:

1. `uv run python -m arf.scripts.utils.prestep $TASK_ID $STEP_ID`
2. Do the step work (see your step's phase below). For skill invocations, spawn a subagent per Rule
   9\.
3. *(step_order ≥ 2 only)* Update `tasks/$TASK_ID/checkpoint.md`: a. Append
   `### Step $STEP_NUMBER — $STEP_ID` to `## Step History` (max 3 sentences: decision made, key
   output file, any caveat for downstream). b. Add downstream-impacting decisions to
   `## Cross-Step Decisions` (GPU tier, dataset variant, budget authorization, risk deviation). Do
   not add routine completions. c. Overwrite `## Next Step Notes` entirely (3-5 sentences for the
   next step-executor by name). d. Update frontmatter: `updated_at` (ISO8601 UTC), `completed_steps`
   (count of completed+skipped steps after this step), `next_step_number`, `next_step_id` (from
   `step_tracker.json`; use `null` when this is the last step). e.
   `uv run flowmark --inplace --nobackup tasks/$TASK_ID/checkpoint.md`
4. `uv run flowmark --inplace --nobackup` on all `.md` files created or modified in this step.
   `uv run ruff check --fix . && uv run ruff format .` on any `.py` files.
5. Stage all step work files **including `checkpoint.md`** (step_order ≥ 2) and `step_tracker.json`.
6. Commit: `$TASK_ID [$STEP_ID]: <description>`
7. `uv run python -m arf.scripts.utils.poststep $TASK_ID $STEP_ID`
8. Return `{"status": "completed", "notes": "<one sentence>"}`.

*(Step 1 `create-branch` is exempt from protocol step 3. The coordinator creates `checkpoint.md`
after `create-branch` completes — see Part A.)*

## Critical Rules

1. Never modify files outside the task folder (except `pyproject.toml`, `uv.lock`, `ruff.toml`, the
   root `.gitignore`, `.gitattributes`, `mypy.ini`). Never create `.gitignore` files inside task
   folders. Infrastructure fixes found during task execution must be deferred to a separate PR on
   `main`.

2. Step numbers are sequential (1, 2, 3, ...) with no gaps, regardless of which canonical steps are
   skipped.

3. Every step follows the prestep/do/poststep cycle — no exceptions.

4. Every CLI command on a task branch must be wrapped with `run_with_logs.py`.

5. Read the relevant specification before creating any file. Do not guess the format — read the
   spec, then write the file, then run the verificator.

6. Run the asset verificator before committing any asset (paper, dataset, etc.).

7. Commit after each step with format: `<task_id> [<step_id>]: <description>`.

8. The coordinator owns the step lifecycle. Step skills (e.g., `/research-papers`,
   `/implementation`, `/generate-suggestions`) only produce output and run their verificator.
   Prestep, commit, step log, and poststep are always handled by the step-executor — never inside
   the step skill.

9. Every skill invocation AND every task step must run in a spawned subagent. When a step uses a
   skill (e.g., `/research-papers`, `/research-internet`, `/implementation`, `/add-paper`,
   `/generate-suggestions`), always spawn a dedicated Agent. Never execute skill logic inline. When
   producing multiple assets of the same type, spawn one subagent per asset.

10. Never override or restrict a skill's instructions when spawning its subagent. Pass the task ID
    and any required context, but do not add constraints like "do NOT add papers" or "skip Phase 3".
    The skill's `SKILL.md` defines what it does — the orchestrator must not second-guess it.

11. Post-merge overview sync happens only on `main` in the main repo. Rebuild, commit, and push
    `overview/` only after the task PR is merged, the worktree is removed, and `main` is updated.
    Never do this from the task branch or task worktree, and do not use `run_with_logs.py` for these
    main-repo commands.

12. NEVER gitignore, exclude, or skip committing files that the task requires as deliverables. If
    output files seem too large for git, ask the user before excluding them.

13. If a subagent fails due to a transient error (API connection failure, timeout, socket error),
    retry once before falling back to inline execution by the orchestrator. Log the failure and
    retry in the step log.

14. NEVER rewrite history on a `task/*` branch. `git lfs migrate import`, `git filter-repo`, and any
    `git push --force` / `--force-with-lease` on a task branch rewrite commits that are already
    shared with the PR. GitHub sees the force-push as the branch disappearing and auto-closes the
    PR, and the audit trail for every earlier step is corrupted. To recover from an oversized file
    caught by `verify_pr_premerge` (PM-E011, 5 MB threshold), compress the file in place and create
    a normal follow-up commit — see Phase 7 Step 3.

15. The coordinator reads ONLY `task.json`, `step_tracker.json`, and `checkpoint.md`. It never reads
    specification files, plan files, research files, or step logs. Those are loaded by
    step-executors only.

16. Each step is executed by a fresh step-executor Agent. The coordinator never executes step work
    inline, except Phase −1 (liveness scan) and Phases 7-9 (PR/merge/overview).

17. Every step-executor at step_order ≥ 2 must update `checkpoint.md` and include it in the step
    commit before calling poststep. Poststep will fail if `checkpoint.md` is absent or inconsistent
    with `step_tracker.json`.

## Writing Code

See `arf/skills/implementation/SKILL.md` for all code writing rules (file location, style
requirements, quality checks, running scripts). The `/implementation` subagent handles code creation
during the implementation step.

Cross-task import rule: tasks must not import from other tasks' `code/` directories. The only
cross-task import mechanism is libraries (registered in `assets/library/`). Non-library code must be
copied into the current task's `code/` directory.

### Planning Phase: Code Design

When the plan calls for writing code, the `plan/plan.md` Step by Step section must specify:

* Which scripts will be created (file names in `code/`)
* What each script does (inputs, outputs, algorithm)
* Which existing project libraries or utilities to reuse
* Any new dependencies needed in `pyproject.toml`

## Step Lifecycle

Every step follows this exact sequence (see also Step-Executor Protocol for the full lifecycle):

```text
1. uv run python -m arf.scripts.utils.prestep  $TASK_ID <step_id>
2. Do the step work
3. (step_order >= 2) Update checkpoint.md — see Step-Executor Protocol step 3
4. CRITICAL: Run `uv run flowmark --inplace --nobackup` on ALL `.md` files created or modified in
   this step — including `step_log.md` and any other files the step-executor writes directly.
   Run `uv run ruff check --fix . && uv run ruff format .` on any `.py` files.
5. Commit the step work (include step_tracker.json and checkpoint.md — see note below)
6. uv run python -m arf.scripts.utils.poststep $TASK_ID <step_id>
   (auto-commits `step_tracker.json`)
```

Never skip prestep or poststep.

**Type-checking task code**: Always invoke mypy as `uv run mypy -p tasks.$TASK_ID.code`, not
`uv run mypy tasks/$TASK_ID/code`. Task folders are Python packages (`tNNNN_slug`), and path-based
mypy invocation triggers duplicate-module-name errors across task folders unless
`--explicit-package-bases` is also set. The `-p` form uses the absolute package path and works in
every task unchanged.

**`step_tracker.json` staging rule**: `prestep` modifies `step_tracker.json` (sets the step status
to `in_progress`). Stage `step_tracker.json` along with your step work files in step 5 so that
`poststep` finds a clean working tree. `poststep` then modifies `step_tracker.json` again (marks the
step `completed`) and auto-commits that change. Do not make a separate manual commit for
`step_tracker.json` — just include it in your normal step work commit.

### Step Log Template

Every `step_log.md` must use this format (from `arf/specifications/logs_specification.md`):

```yaml
---
spec_version: "3"
task_id: "$TASK_ID"
step_number: <N>
step_name: "<step-id>"
status: "completed"
started_at: "<ISO8601 UTC>"
completed_at: "<ISO8601 UTC>"
---
```

Mandatory sections: `## Summary` (min 20 words), `## Actions Taken` (numbered list, min 2 items),
`## Outputs` (bullet list of files), `## Issues` (problems or "No issues encountered.").

**Skipped steps**: Use the `skip_step.py` utility to mark steps as skipped in batch. It creates the
step log directory, writes a minimal `step_log.md` with correct frontmatter and mandatory sections,
and updates `step_tracker.json`:

```bash
uv run python -m arf.scripts.utils.skip_step $TASK_ID \
  research-papers "No relevant papers in corpus for this task." \
  research-code "No prior task code relevant to this work."
```

Do NOT run prestep or poststep for skipped steps. Commit all skipped step logs **before** running
prestep for the next active step — prestep requires a clean working tree.

## Steps

### Phase 0: Preparation (create-branch step-executor scope)

Phase 0 critical rule: use only the Read tool to read files. Do not run any CLI commands (`uv run`,
`python`, aggregator scripts) before the worktree is created. Commands wrapped with
`run_with_logs.py` that run before the worktree write logs to the main repo directory, where they
are never committed. All CLI commands must wait until after `create-branch` (Phase 1).

1. Read `tasks/$TASK_ID/task.json` using the Read tool — understand the objective, dependencies,
   expected assets, and `task_types`. If it contains `long_description_file`, also read the
   referenced markdown file from the task root before deciding the step plan.

2. Read `arf/specifications/task_steps_specification.md` using the Read tool — understand the
   canonical steps and which are required vs optional.

3. Read asset type specifications in `meta/asset_types/` using the Read tool for each expected asset
   type listed in `task.json`.

4. Note the dependency list and `task_types` from `task.json` — these will be verified via CLI
   commands after the worktree is created.

### Phase 1: Preflight

#### Step: `create-branch`

```bash
uv run python -m arf.scripts.utils.worktree create $TASK_ID
```

The `create` command prints the worktree path to stdout. **Change your working directory to the
worktree path immediately.** All subsequent steps, commands, and file operations must happen inside
the worktree:

```bash
cd <printed_worktree_path>
```

Now run prestep (which auto-creates a minimal `step_tracker.json`):

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID create-branch
```

#### Step planning (inside worktree, before writing `step_tracker.json`)

Now that all commands run inside the worktree, verify dependencies and determine the step list:

1. For each dependency in `task.json`, run the task aggregator to verify it is completed and
   understand what it produced:

   ```bash
   uv run python -u -m arf.scripts.aggregators.aggregate_tasks \
     --format json --detail short --ids <dep_id_1> <dep_id_2> ...
   ```

   Returns `{"task_count": N, "tasks": [{...}, ...]}`. Each task object has: `task_id`, `name`,
   `short_description`, `status`, `task_types`, `dependencies`. Only fetch dependency tasks, not the
   full project task list.

2. Check current project spend and budget left, but only if this task's task types can incur paid
   external costs. Load the task type definitions via `aggregate_task_types.py` and inspect the
   `has_external_costs` field on each entry listed in `task.json` `task_types`:

   ```bash
   uv run python -u -m arf.scripts.aggregators.aggregate_task_types --format json
   ```

   * If at least one listed task type has `has_external_costs: true`, or if `task_types` is empty
     and the task fallback inference (see step 3) classifies this as an experiment-style task, run
     the cost aggregator:

     ```bash
     uv run python -u -m arf.scripts.aggregators.aggregate_costs --format json --detail full
     ```

     Returns `{"budget": {...}, "summary": {...}, "tasks": [...], "skipped_tasks": [...]}`.

     If `data["summary"]["stop_threshold_reached"]` is `true` or
     `data["summary"]["budget_left_before_stop_usd"] <= 0`, create
     `tasks/$TASK_ID/intervention/project_budget_exhausted.md` and STOP. Record the remaining budget
     for the planning and setup-machines prompts.

   * If every listed task type has `has_external_costs: false`, skip the cost aggregator and the
     budget gate entirely. Mechanical, analytical, and retrieval task types (download-paper,
     deduplication, brainstorming, data-analysis, correction, write-library, etc.) cannot move the
     project spend total and must not be blocked on it. Record a single log line in the step log
     noting that the budget gate was skipped because no listed task type has external costs.

   The budget gate applies per task, not per project. A project with `total_budget: 0.0` must still
   be able to run cost-free task types without producing an intervention file.

3. Determine which steps this task needs. Always include all 7 required steps. For the 8 optional
   steps, use task type definitions to decide inclusion:

   Always required (include in every task):

   * `create-branch`, `check-deps`, `init-folders` — preflight
   * `implementation` — implementation
   * `results`, `suggestions`, `reporting` — analysis and reporting

   Optional (inclusion depends on task type):

   * `research-papers`, `research-internet`, `research-code` — research
   * `planning` — planning
   * `setup-machines`, `teardown` — remote compute
   * `creative-thinking`, `compare-literature` — analysis

   Task-type-based step selection:

   If `task_types` in `task.json` is non-empty:

   * Load the task type definitions via the aggregator:

     ```bash
     uv run python -u -m arf.scripts.aggregators.aggregate_task_types --format json
     ```

     Returns `{"task_types": [...]}`. Each entry has `task_type_id`, `optional_steps`,
     `instruction`. Note: this aggregator does NOT support `--detail`.

   * For each type in `task_types`, read its `optional_steps` list.

   * Compute the union of all `optional_steps` across all matched types.

   * Include those optional steps. Skip optional steps not in the union.

   * Note: if `setup-machines` is included, `teardown` must also be included.

   * The agent may still apply judgment — `optional_steps` is guidance, not absolute. If a step
     seems clearly needed despite not being listed (or clearly unnecessary despite being listed),
     adjust with a brief note in the step description.

   * For `correction` tasks, always inspect the task text directly before finalizing the step list.
     The default `optional_steps` may be empty, but:

     * include `research-code` if the correction depends on understanding prior code, assets, or
       existing outputs

     * include `research-papers` or `research-internet` only if the correction requires external
       factual validation

     * include `planning` if the correction spans multiple artifacts, introduces replacement assets,
       or uses non-trivial `file_changes`

     * skip research and planning for straightforward corrections whose target and fix are already
       explicit in the task text

   If `task_types` is empty (unclassified task), fall back to these criteria:

   * `research-papers` — include if the task involves methods, techniques, or evaluations where
     existing papers in the corpus may inform the approach. Skip for mechanical tasks (downloading,
     infrastructure, deduplication).

   * `research-internet` — include if the task requires knowledge not already captured in the paper
     corpus or prior tasks (new tools, APIs, recent publications). Skip for tasks that operate
     entirely on local data or prior work.

   * `research-code` — include if prior tasks produced code, libraries, datasets, or other reusable
     assets that might inform this task. Skip for the first few tasks in a project or when the task
     is independent of prior work.

   * `planning` — include for tasks with non-trivial implementation requiring design decisions, cost
     estimation, or multi-step execution. Skip for simple mechanical tasks (downloading a single
     file, deduplication).

   * `setup-machines` — include if the plan requires remote compute (GPU training, large-scale
     inference, distributed processing). Skip for local-only tasks (downloads, data processing,
     analysis).

   * `teardown` — include if and only if `setup-machines` is included.

   * `creative-thinking` — include for experiment tasks where alternative approaches or
     out-of-the-box analysis could yield insights. Skip for mechanical tasks (downloading, data
     conversion, infrastructure).

   * `compare-literature` — include for experiment tasks that produce quantitative results
     comparable to published results. Skip for tasks that don't produce performance metrics.

4. Plan the step list:

   * Sequential step numbers (1, 2, 3, ...) with no gaps
   * Canonical step IDs as the `name` field
   * All steps set to `"pending"`
   * A `description` field tailored to this specific task

   The first three steps are always:

   1. `create-branch`
   2. `check-deps`
   3. `init-folders`

   Then the task-specific steps follow in canonical phase order (research, planning, execution,
   analysis, reporting). Number them sequentially starting from 4.

   For every canonical optional step NOT included in the task's step list, add it to
   `step_tracker.json` with status `"skipped"` and a brief description explaining why. All canonical
   optional steps must appear in `step_tracker.json` — either as `"pending"` / `"completed"` (if
   executed) or `"skipped"` (if not).

   Example for a `data-analysis` task (optional steps: research-papers, research-code, planning,
   creative-thinking):

   ```text
   1: create-branch
   2: check-deps
   3: init-folders
   4: research-papers
   5: research-code
   6: planning
   7: implementation
   8: creative-thinking
   9: results
   10: suggestions
   11: reporting
   ```

   Example for a `download-dataset` task (optional steps: planning):

   ```text
   1: create-branch
   2: check-deps
   3: init-folders
   4: planning
   5: implementation
   6: results
   7: suggestions
   8: reporting
   ```

   Example for an `answer-question` task (optional steps: research-papers, research-internet,
   research-code, planning, creative-thinking):

   ```text
   1: create-branch
   2: check-deps
   3: init-folders
   4: research-papers
   5: research-internet
   6: research-code
   7: planning
   8: implementation
   9: creative-thinking
   10: results
   11: suggestions
   12: reporting
   ```

Write the full `step_tracker.json` with all steps planned above (this overwrites the minimal tracker
created by prestep).

Write `logs/steps/001_create-branch/branch_info.txt`:

```text
branch: task/$TASK_ID
base_branch: main
base_commit: <commit_hash>
worktree_path: <worktree_path>
created_at: <ISO8601_UTC>
```

Commit and run poststep. The coordinator then creates `checkpoint.md` — see Part A.

#### Step: `check-deps`

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID check-deps
```

Prestep runs `verify_task_dependencies.py` automatically. Write the output to
`logs/steps/002_check-deps/deps_report.json`:

```json
{
  "task_id": "<task_id>",
  "checked_at": "<ISO8601_UTC>",
  "result": "passed",
  "dependencies": [
    {"task_id": "<dep_id>", "status": "completed", "satisfied": true}
  ],
  "errors": 0,
  "warnings": 0
}
```

Update `checkpoint.md`, commit, and run poststep.

#### Step: `init-folders`

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID init-folders
```

Create the mandatory task folder structure using the init script:

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
  uv run python -m arf.scripts.utils.init_task_folders $TASK_ID \
  --step-log-dir tasks/$TASK_ID/logs/steps/003_init-folders/
```

The script creates all required directories, reads `task.json` `expected_assets` for asset
subdirectories, and adds `.gitkeep` to every empty directory. The `--step-log-dir` flag causes the
script to automatically write `tasks/$TASK_ID/logs/steps/003_init-folders/folders_created.txt`.

#### Aggregator cache (part of init-folders step)

Still within the `init-folders` step (before committing it), populate the aggregator cache so
subagents do not re-fetch across phases. Commands use shell redirection, so wrap with `bash -c`:

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
  bash -c "mkdir -p tasks/$TASK_ID/ctx && \
    uv run python -u -m arf.scripts.aggregators.aggregate_task_types \
      --format json > tasks/$TASK_ID/ctx/task_types.json && \
    uv run python -u -m arf.scripts.aggregators.aggregate_costs \
      --format json --detail full > tasks/$TASK_ID/ctx/costs.json && \
    uv run python -u -m arf.scripts.aggregators.aggregate_tasks \
      --format json --detail short > tasks/$TASK_ID/ctx/tasks.json && \
    uv run python -u -m arf.scripts.aggregators.aggregate_metrics \
      --format json --detail full > tasks/$TASK_ID/ctx/metrics.json && \
    uv run python -u -m arf.scripts.aggregators.aggregate_suggestions \
      --format json --detail full > tasks/$TASK_ID/ctx/suggestions.json"
```

Stage both the created directories (including `.gitkeep` files) and the step log directory
(`tasks/$TASK_ID/logs/steps/003_init-folders/`), update `checkpoint.md`, then commit and run
poststep. Do not stage `tasks/$TASK_ID/ctx/` — these cache files are gitignored and intentionally
local-only; they are consumed within this session but not committed to the branch.

These cache files are the source of truth for this task run. Subagents read them instead of
re-running aggregators. Exception: if this task adds or edits `meta/` content (new metric, category,
task type), re-run the affected aggregator via `run_with_logs` and overwrite the corresponding
`ctx/` file before planning.

### Phase 2: Research

#### Step: `research-papers` (optional)

Include this step if the task type's `optional_steps` lists `research-papers` or if task-content
judgment says it is needed. Skip for task types that exclude it (e.g., `write-library`,
`download-dataset`, `infrastructure-setup`) unless the task text clearly requires literature
validation. For `correction` tasks, include this only when the correction itself depends on paper
evidence.

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID research-papers
```

Spawn a subagent to execute the `/research-papers` skill:

```text
Use the Agent tool to launch a subagent with this prompt:
"Execute the /research-papers skill for task $TASK_ID.
Read arf/skills/research-papers/SKILL.md and follow all steps."
```

The subagent will review existing papers and write `research/research_papers.md`. After the subagent
completes, verify the output exists and the verificator passes:

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
  uv run python -m arf.scripts.verificators.verify_research_papers $TASK_ID
```

Update `checkpoint.md`, write `logs/steps/NNN_research-papers/step_log.md`, commit, and run
poststep. If skipped, the step must still appear in `step_tracker.json` with status `"skipped"`.

#### Step: `research-internet` (optional)

Include this step if the task type's `optional_steps` lists `research-internet` or if task-content
judgment says it is needed. Skip for task types that exclude it (e.g., `download-dataset`,
`download-paper`, `deduplication`) unless the task text clearly requires new external information.
For `correction` tasks, include this only when the correction depends on current external facts,
documentation, or recent changes not already captured in the project.

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID research-internet
```

Spawn a subagent to execute the `/research-internet` skill:

```text
Use the Agent tool to launch a subagent with this prompt:
"Execute the /research-internet skill for task $TASK_ID.
Read arf/skills/research-internet/SKILL.md and follow all steps."
```

The subagent will search the internet and write `research/research_internet.md` with a
`## Discovered Papers` section listing all new papers found. After the subagent completes, verify
the output exists and the verificator passes:

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
  uv run python -m arf.scripts.verificators.verify_research_internet $TASK_ID
```

Update `checkpoint.md`, write step log, commit, and run poststep. If skipped, the step must still
appear in `step_tracker.json` with status `"skipped"`.

**Paper addition from Discovered Papers**: After the research-internet step completes successfully,
parse the `## Discovered Papers` section of `research/research_internet.md`. For each paper listed
that is not already in the corpus (check via `aggregate_papers.py` comparing DOIs and normalized
titles), spawn an `/add-paper` subagent. Spawn subagents for ALL discovered papers, max 3 running
concurrently. If more than 3 papers need adding, queue the remainder and spawn each as a slot frees.
Every discovered paper must be attempted. These paper-addition subagents run **in parallel with
subsequent steps** — do not wait for them to finish before proceeding to `research-code` or
`planning`. Only the `compare-literature` step (if included) and the final `reporting` step should
wait for all paper subagents to complete. Track paper-addition subagents in a local list and check
completion before those later steps. If any paper-addition subagent fails (API error, timeout,
etc.), log the failure and attempt the paper addition inline during the `reporting` step. Before
running poststep for any step, run `git status` to check for untracked files from parallel agents
(e.g., paper downloads in `assets/paper/`). If present, stage and commit them before running
poststep so the working tree is clean.

#### Step: `research-code` (optional)

Include this step if prior tasks produced code, libraries, datasets, answers, models, predictions,
or other reusable assets that might inform this task. This step is especially important for
`correction` tasks when the target artifact, replacement artifact, or partial file overlay must be
understood before the fix is applied. If skipped, the step must still appear in `step_tracker.json`
with status `"skipped"`.

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID research-code
```

Spawn a subagent to execute the `/research-code` skill:

```text
Use the Agent tool to launch a subagent with this prompt:
"Execute the /research-code skill for task $TASK_ID.
Read arf/skills/research-code/SKILL.md and follow all steps."
```

The subagent will review existing libraries, answer assets, and completed tasks, then write
`research/research_code.md`. After the subagent completes, verify the output exists and the
verificator passes:

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
  uv run python -m arf.scripts.verificators.verify_research_code $TASK_ID
```

#### Summarize research

After all research steps complete (research-papers, research-internet, research-code), spawn a
subagent to compress the research files into a compact summary:

```text
Use the Agent tool to launch a subagent with this prompt:
"Execute the /research-summarize skill for task $TASK_ID.
Read arf/skills/research-summarize/SKILL.md and follow all steps."
```

This produces `research/research_summary.md` (~5–8 KB). Planning and implementation agents load this
file instead of the full research files. This step is lightweight and does not need its own
step_tracker entry — run it inline after the last research step completes.

Log this under the last research step that actually ran (research-code, or research-internet /
research-papers if research-code was skipped): append a note to that step's
`tasks/$TASK_ID/logs/steps/NNN_<step>/step_log.md`. Update `checkpoint.md`. Stage both that step log
and `tasks/$TASK_ID/research/research_summary.md`, then commit and run poststep.

### Phase 3: Planning

#### Step: `planning` (optional)

Include this step if the task type's `optional_steps` lists `planning` or if task-content judgment
says planning is needed. Skip for task types that exclude it (e.g., `download-paper`,
`brainstorming`) unless the task text clearly calls for multi-step design work. For `correction`
tasks, include planning when the correction spans multiple targets, introduces replacement assets,
or needs careful verification of file overlays and aggregator behavior.

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID planning
```

Spawn a subagent to execute the `/planning` skill. Include any user-provided context about budget
authorization, speed preferences, special instructions, and the current budget summary from
`aggregate_costs.py` in the prompt:

```text
Use the Agent tool to launch a subagent with this prompt:
"Execute the /planning skill for task $TASK_ID.
Read arf/skills/planning/SKILL.md and follow all steps.
[Include any user context here, e.g.: The user authorized up to $40 for
faster compute. The user wants to prioritize speed over cost savings.]"
```

The subagent will synthesize all research outputs, scan existing answer assets via the answer
aggregator, and write `plan/plan.md` with all 11 mandatory sections. The plan's `## Step by Step`
must cover only implementation work — ending at "compute metrics and produce charts." Results
writing, suggestions, and compare-literature are orchestrator steps managed by execute-task — do not
include them in the plan.

After the subagent completes, verify the output exists and the verificator passes:

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
  uv run python -m arf.scripts.verificators.verify_plan $TASK_ID
```

Update `checkpoint.md`, write step log, commit, and run poststep. If skipped, the step must still
appear in `step_tracker.json` with status `"skipped"`.

### Phase 4: Execution

#### Step: `setup-machines` (optional)

Include this step ONLY when the plan requires remote compute (GPU training, large-scale inference,
distributed processing). Skip for local-only tasks.

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID setup-machines
```

Spawn a subagent to execute the `/setup-remote-machine` skill. Include any user-provided budget
authorization, speed preferences, and the current budget summary from `aggregate_costs.py` in the
prompt:

```text
Execute the /setup-remote-machine skill for task $TASK_ID.
Read arf/skills/setup-remote-machine/SKILL.md and follow all steps
through Phase 5 (Prepare the Environment).
[Include any user context here, e.g.: The user authorized up to $40
for faster compute. Prefer speed over cost savings.]
```

After the subagent completes, verify:

* `machine_log.json` exists in the step log directory with all required fields per
  `arf/specifications/remote_machines_specification.md`

* SSH connection and GPU verified (check the `gpu_verified` field)

Update `checkpoint.md`, write step log, commit, and run poststep. If skipped, the step must still
appear in `step_tracker.json` with status `"skipped"`.

#### Step: `implementation`

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID implementation
```

Spawn a subagent to execute the `/implementation` skill:

Use the Agent tool to launch a subagent with this prompt:

```text
Execute the /implementation skill for task $TASK_ID.
Read arf/skills/implementation/SKILL.md and follow all steps.
```

After the subagent completes, verify that all expected assets from `task.json` exist and pass their
verificators. Also review the subagent's final `Requirement Completion Checklist`: every `REQ-*`
item must be marked `done`, or the task must have a documented intervention explaining why it is
`partial` or `blocked`.

For experiment tasks with multiple variants, check cumulative API cost after each variant run
completes. If the plan specifies a budget cap, compare the running total against it. If the cap is
exceeded, stop further variant runs and document the overrun in `results/costs.json` `note` field.

Update `checkpoint.md`, write step log, commit, and run poststep.

#### Step: `teardown` (optional)

Include if and only if `setup-machines` is included. Skip for local-only tasks. If all GPU work
completed during implementation, run teardown IMMEDIATELY after implementation — before
compare-literature, results, or suggestions. When the task used multiple machines and some were
already destroyed during implementation, the teardown step handles only machines not yet destroyed
and updates final cost tracking in `results/costs.json` and `results/remote_machines_used.json`.

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID teardown
```

Spawn a subagent to execute the Teardown Protocol from the `/setup-remote-machine` skill:

```text
Execute the Teardown Protocol from arf/skills/setup-remote-machine/SKILL.md
for task $TASK_ID. Download all results, destroy the instance, and update
machine_log.json, remote_machines_used.json, and costs.json.
```

After the subagent completes, verify:

* `machine_log.json` has a non-null `destroyed_at` timestamp

* `results/remote_machines_used.json` and `results/costs.json` reflect the machine usage

* Run `verify_machines_destroyed.py`:

  ```bash
  uv run python -m arf.scripts.verificators.verify_machines_destroyed \
    --task-id $TASK_ID
  ```

Update `checkpoint.md`, write step log, commit, and run poststep.

### Phase 5: Analysis

#### Step: `creative-thinking` (optional)

Out-of-the-box analysis and alternative approaches. Update `checkpoint.md`. If skipped, the step
must still appear in `step_tracker.json` with status `"skipped"`.

#### Step: `results`

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID results
```

Write all results files:

* `results/results_summary.md` — must contain exactly these mandatory sections: `## Summary` (2-3
  sentences), `## Metrics` (min 3 bullet points with specific numbers), `## Verification` (list
  verificator outcomes)

* `results/results_detailed.md` — must contain these mandatory sections: `## Summary` (2-5
  sentences), `## Methodology` (machine specs, runtime, timestamps), `## Verification` (verificator
  results), `## Limitations` (constraints and caveats), `## Files Created` (bullet list of outputs),
  `## Task Requirement Coverage` (must be the final section). For experiment tasks, also include
  `## Examples` (min 10 concrete instances from prediction files — never fabricate examples). Each
  example must show the complete input-output pair: actual input given to the system and the actual
  raw output it produced, both in fenced code blocks. A summary table (e.g., item/prediction/correct
  columns) is NOT sufficient — the reader must see exactly what went in and what came out. For
  instance, if the task calls an LLM, show the actual prompt and the actual model response; if the
  task runs a classifier, show the feature vector and the raw prediction. Read
  `arf/specifications/task_results_specification.md` § "## Examples" for full requirements. Use
  `spec_version: "2"` in the YAML frontmatter (this is the results_detailed.md format version, not
  the specification document version). Before writing, re-read `tasks/$TASK_ID/task.json` and
  `plan/plan.md` `## Task Requirement Checklist`. This file must summarize all result artifacts
  produced by the task, not just the highlights. If a dedicated file is too long to inline fully,
  include the key findings in `results_detailed.md` and link to the full file. In the
  `## Task Requirement Coverage` section:

  * quote the operative task text from `task.json` plus the resolved long description verbatim
  * list every concrete `REQ-*` item from the plan
  * give a direct answer/result for each item
  * mark each item as `Done`, `Partial`, or `Not done`
  * point to the exact file/table/image/asset path that supports the claim

* `results/metrics.json` — only registered project metrics from `meta/metrics/` (e.g., `f1_all`,
  `accuracy_se2`, `efficiency_inference_cost_per_item_usd`). Two formats are valid:

  * Legacy flat format for a single metrics set:

    ```json
    {"f1_all": 82.3}
    ```

  * Explicit variant format for multiple metrics sets produced by one task:

    ```json
    {
      "variants": [
        {
          "variant_id": "model-a_prompt-short",
          "label": "Model A + short prompt",
          "dimensions": {"model": "model-a", "prompt": "short"},
          "metrics": {"f1_all": 82.3}
        }
      ]
    }
    ```

  Use the explicit variant format whenever one task compares multiple models, prompts, dataset
  sizes, hyperparameter settings, or other conditions that each produce their own metric set. Write
  `{}` if this task did not measure any registered metrics. Task-specific operational data (corpus
  sizes, download stats, counts) belongs in `results_detailed.md` or dedicated result files, NOT
  here. To discover available registered metric keys:

  ```bash
  uv run python -u -m arf.scripts.aggregators.aggregate_metrics --format ids
  ```

  To see full details (name, description, unit, datasets):

  ```bash
  uv run python -u -m arf.scripts.aggregators.aggregate_metrics --format json --detail full
  ```

  Run `verify_task_metrics.py` to validate before committing.

* `results/costs.json` — follow `arf/specifications/task_results_specification.md`. Required fields:
  `total_cost_usd` and `breakdown`. For zero-cost tasks: `{"total_cost_usd": 0, "breakdown": {}}`

* `results/remote_machines_used.json` — `[]` if none

* `results/images/` — charts and graphs (if applicable)

**Metrics cross-check**: Before writing `results_summary.md` or `results_detailed.md`, read
`results/metrics.json`. Verify that every number quoted in the markdown exactly matches the JSON
source. If any metric is `0.0` or `null` for a value that should have a real measurement,
investigate the implementation output before writing results. Numbers in markdown and JSON must be
identical — no rounding, no "approximately."

**Charts**: For experiment tasks, `results/images/` must contain at least 2 charts (e.g., metric
comparison bar chart, per-category breakdown). Every PNG in `results/images/` must be embedded in
`results_detailed.md` with `![description](images/filename.png)` and a 1-3 sentence description of
what the chart shows and the key takeaway.

Do not treat auxiliary result files as a substitute for `results_detailed.md`. The reader should be
able to understand the full output surface from `results_detailed.md` alone, using relative links
only for drill-down detail.

**Plan assumption check**: Re-read the plan's Objective and Approach sections. If actual results
contradict any stated assumption, hypothesis, or prior-task claim, document the discrepancy
prominently in `results_detailed.md` under `## Analysis`. Contradicted assumptions are findings —
they must be reported, not buried.

Update `checkpoint.md`, write step log, commit, and run poststep.

#### Step: `compare-literature` (optional)

Include for experiment tasks that produce quantitative results comparable to published results. Skip
for tasks that do not produce performance metrics. If skipped, the step must still appear in
`step_tracker.json` with status `"skipped"`.

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID compare-literature
```

Spawn a subagent to execute the `/compare-literature` skill:

```text
Use the Agent tool to launch a subagent with this prompt:
"Execute the /compare-literature skill for task $TASK_ID.
Read arf/skills/compare-literature/SKILL.md and follow all steps."
```

The subagent will compare results against published results and write
`results/compare_literature.md`. After the subagent completes, verify the output exists and the
verificator passes:

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
  uv run python -m arf.scripts.verificators.verify_compare_literature $TASK_ID
```

Fix all errors. Re-run until zero errors. Update `checkpoint.md`, write step log, commit, and run
poststep.

### Phase 6: Reporting

#### Step: `suggestions`

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID suggestions
```

Spawn a subagent to execute the `/generate-suggestions` skill:

```text
Use the Agent tool to launch a subagent with this prompt:
"Execute the /generate-suggestions skill for task $TASK_ID.
Read arf/skills/generate-suggestions/SKILL.md and follow all steps."
```

The subagent will review results, check existing suggestions for duplicates, and write
`results/suggestions.json`. After the subagent completes, verify the output exists and the
verificator passes:

```bash
uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
  uv run python -m arf.scripts.verificators.verify_suggestions $TASK_ID
```

Fix all errors. Re-run until zero errors. Update `checkpoint.md`, write step log, commit, and run
poststep.

#### Step: `reporting`

```bash
uv run python -m arf.scripts.utils.prestep $TASK_ID reporting
```

1. Run ALL relevant verificators with `run_with_logs.py`:

   * `verify_task_file.py`
   * `verify_task_dependencies.py`
   * `verify_suggestions.py`
   * `verify_task_metrics.py`
   * `verify_task_results.py`
   * `verify_task_folder.py`
   * `verify_logs.py`
   * Asset verificators for each produced asset type (all use `--task-id $TASK_ID`; pass a specific
     asset ID as positional arg, or omit to verify all assets of that type):
     * `verify_paper_asset.py --task-id $TASK_ID [paper_id]`
     * `verify_predictions_asset.py --task-id $TASK_ID [predictions_id]`,
       `verify_predictions_description.py --task-id $TASK_ID [predictions_id]`,
       `verify_predictions_details.py --task-id $TASK_ID [predictions_id]`
     * `verify_model_asset.py --task-id $TASK_ID [model_id]`
     * `verify_library_asset.py --task-id $TASK_ID [library_id]`
   * `verify_corrections.py $TASK_ID` (if corrections/ contains files)
   * `verify_research_papers.py` (if paper research was done)
   * `verify_research_internet.py` (if internet research was done)
   * `verify_compare_literature.py` (if compare-literature step was done)
   * `verify_machines_destroyed.py` (if remote machines were used)

2. Capture raw agent session transcripts and write the session capture report. Run the shared
   utility through `run_with_logs.py`:

   ```bash
   uv run python -m arf.scripts.utils.run_with_logs --task-id $TASK_ID -- \
     uv run python -m arf.scripts.utils.capture_task_sessions --task-id $TASK_ID
   ```

   The capture utility scans supported CLI transcript roots (currently Codex and Claude Code),
   copies every matching JSONL transcript into `logs/sessions/`, and writes
   `logs/sessions/capture_report.json`. If no matching transcript is found, proceed — the
   verificators emit a warning, but the reporting step still records what was checked.

3. Update `task.json`: set `status` to `"completed"` and set `end_time`. Note: `start_time` is
   already set by `worktree create` — do not overwrite it.

4. Update `checkpoint.md` (this is the final update: set `next_step_number` and `next_step_id` to
   `null`, update `completed_steps`). Write step log. Commit and run poststep.

### Phase 7: PR and Merge

*(Executed inline by the coordinator after all steps complete.)*

1. Push the task branch:

   ```bash
   git push -u origin task/$TASK_ID
   ```

2. Create PR:

   ```bash
   gh pr create --title "$TASK_ID: <task name>" --body "..."
   ```

   PR body must contain: Summary (2-3 bullets), Assets Produced, Verification (all verificators
   passed).

3. Run the pre-merge verificator:

   ```bash
   uv run python -m arf.scripts.verificators.verify_pr_premerge $TASK_ID --pr-number <number>
   ```

   This checks file isolation, branch naming, PR format, task state, sensitive files, large files,
   merge conflicts, and runs all sub-verificators. If errors are found: fix the issues, commit,
   re-push, and re-run until zero errors. Do NOT merge with errors.

   **Recovering from PM-E011 (large file)**: the threshold is 5 MB (`LARGE_FILE_THRESHOLD_BYTES` in
   `verify_pr_premerge.py`). Fix it in place with a forward commit — never rewrite history:

   * For PDFs, rasterize with PyMuPDF at about 100 DPI / JPEG quality 60 and overwrite the file at
     its original path inside the task folder.
   * For other binary assets, reduce the file at its source (e.g., re-export at a lower resolution,
     recompress) and overwrite the path.
   * Then
     `git add <path> && git commit -m "$TASK_ID [reporting]: Compress <file> to satisfy PM-E011"`
     and `git push`. Re-run `verify_pr_premerge` until clean.

   NEVER run `git lfs migrate import`, `git filter-repo`, or `git push --force*` on the task branch.
   Those rewrite history shared with the PR and cause GitHub to auto-close it (see Critical Rule
   14).

4. If the PR has merge conflicts (common when parallel tasks merge to main during execution), merge
   `origin/main` into the task branch:

   ```bash
   git fetch origin main
   git merge origin/main
   # Resolve conflicts — for task.json, keep the task branch version
   git add <resolved files>
   git commit -m "$TASK_ID [reporting]: Merge main to resolve conflicts"
   git push
   ```

   Re-run `verify_pr_premerge` after resolving conflicts.

5. Merge with merge commit (preserve history):

   ```bash
   gh pr merge <number> --merge
   ```

6. Return to the main repo and remove the worktree:

   ```bash
   cd <main_repo_root>
   git checkout main
   uv run python -m arf.scripts.utils.worktree remove $TASK_ID
   git pull --ff-only
   ```

7. Clean up orphaned untracked files in the task folder. Commands wrapped with `run_with_logs.py`
   that ran before the worktree was created (Phase 0 violation) or after the final commit leave
   untracked log files on `main`. Delete them:

   ```bash
   git clean -fd tasks/$TASK_ID/logs/commands/
   ```

   If `git clean` reports files it would delete, that confirms orphaned logs existed. If there is
   nothing to clean, this is a harmless no-op.

### Phase 8: Final Verification

*(Executed inline by the coordinator.)*

Run the completion verificator from the main repo (after worktree removal and `git pull`) to confirm
everything is in order:

```bash
uv run python -m arf.scripts.verificators.verify_task_complete $TASK_ID
```

This checks: task status, timestamps, all steps finished, mandatory dirs and files, expected assets,
git branch exists, PR merged, no files modified outside task folder, and runs all sub-verificators
(task file, dependencies, asset verificators). Fix any errors before considering the task done.

The reporting-step capture (Phase 6 Step 2) is the final session capture for the task. Do NOT re-run
`capture_task_sessions` here — a completed task folder is immutable (see `arf/README.md`
"Immutability of Completed Tasks"), and any commit landing in `tasks/$TASK_ID/logs/sessions/` after
this point mutates a task whose branch has already been merged.

### Phase 9: Overview Sync

*(Executed inline by the coordinator.)*

After `verify_task_complete.py` passes, refresh the generated overview from the main repo on `main`:

1. Confirm you are in the main repo root on branch `main`.

2. Rebuild the overview:

   ```bash
   uv run python -u -m arf.scripts.overview.materialize
   ```

3. Review the resulting diff. If `overview/` is unchanged, skip the commit and push.

4. If `overview/` changed:

   * Stage only `overview/`. Do not include unrelated local changes from the main repo.

   * If `overview/` already contains unrelated user edits, stop and resolve that conflict instead of
     overwriting them blindly.

   * Commit directly on `main` as a separate maintenance commit, for example:

     ```bash
     git add overview
     git commit -m "overview: refresh after $TASK_ID"
     ```

   * Push the updated `main` branch:

     ```bash
     git push origin main
     ```

## Done When

* All steps in `step_tracker.json` are `completed` (or `skipped` with documented reason)

* `task.json` has `status: "completed"` with `start_time` (set by `worktree create`) and `end_time`

* All expected assets exist and pass their verificators

* `results/results_detailed.md` exhaustively covers every concrete task requirement and ends with
  `## Task Requirement Coverage`

* All verificators pass with zero errors

* PR is created, reviewed, and merged to `main`

* Task branch is pushed (kept for audit trail)

* `overview/` has been rebuilt from the main repo on `main`, and any resulting diff is committed and
  pushed as a separate `main` commit

## Forbidden

* NEVER modify files outside the task folder on a task branch

* NEVER skip prestep or poststep for any step

* NEVER use non-sequential step numbers in `step_tracker.json`

* NEVER create an asset without reading its specification first

* NEVER commit an asset without running its verificator

* NEVER run CLI commands on a task branch without `run_with_logs.py`

* NEVER use `run_with_logs.py` before the worktree is created (Phase 0) — logs land on `main` and
  are never committed

* NEVER use `run_with_logs.py` after the PR is merged (Phases 8-9) — logs land on `main` and are
  never committed

* NEVER claim "no papers to add" without verifying every cited paper exists in the corpus

* NEVER make a separate commit just for `step_tracker.json` — stage it with your step work; poststep
  auto-commits the completion update

* NEVER modify infrastructure files (arf/, .claude/, specs) on a task branch

* NEVER override or restrict a skill's behavior when spawning its subagent

* NEVER create multiple assets inline — spawn one subagent per asset

* NEVER execute skill logic directly in the orchestrator — always use a subagent

* NEVER place Python files or scripts outside `code/` — all code goes in `tasks/$TASK_ID/code/`

* NEVER commit Python code without running `ruff check --fix`, `ruff format`, and `mypy`

* NEVER run `worktree.py create` or `worktree.py remove` from inside a worktree. Run these from the
  main repo only

* NEVER run `git lfs migrate import`, `git filter-repo`, or `git push --force` /
  `--force-with-lease` on a `task/*` branch — they rewrite history shared with the PR, cause GitHub
  to auto-close it, and corrupt the audit trail. To recover from PM-E011 (large file), compress the
  file in place and commit forward (see Critical Rule 14 and Phase 7 Step 3)

* NEVER rebuild, commit, or push `overview/` from a task branch or task worktree

* NEVER have the coordinator read specification files, plan files, research files, or step logs —
  those are step-executor scope only (Rule 15)

* NEVER execute step work inline in the coordinator — always spawn a step-executor Agent (Rule 16)

* NEVER commit a step without updating `checkpoint.md` for step_order ≥ 2 (Rule 17)
