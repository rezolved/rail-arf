# Checkpoint Specification

**Version**: 1

This specification defines the format, structure, and content requirements for
`tasks/$TASK_ID/checkpoint.md` — the handoff document written by each step-executor and read by the
coordinator at the start of every new session.

The coordinator reads only three files: `task.json`, `step_tracker.json`, and `checkpoint.md`. The
checkpoint carries the accumulated decisions and step summaries that the coordinator needs to spawn
an informed step-executor, without the coordinator ever loading specification files, plan files, or
step logs.

* * *

## Location

```
tasks/$TASK_ID/checkpoint.md
```

Committed to the task branch. Created by the coordinator after step 1 completes. Updated by every
subsequent step-executor as the last action before running poststep.

* * *

## Schema

### YAML Frontmatter

Every `checkpoint.md` begins with a YAML frontmatter block delimited by `---`:

```yaml
---
spec_version: "1"
task_id: "t0007_build_model_qwen35_moe"
updated_at: "2026-06-23T14:32:00Z"
completed_steps: 4
next_step_number: 5
next_step_id: "planning"
---
```

| Field | Type | Description |
| --- | --- | --- |
| `spec_version` | string | Always `"1"` for files conforming to this version. |
| `task_id` | string | Task folder name (e.g. `t0007_build_model_qwen35_moe`). Must match the folder name exactly. |
| `updated_at` | string | ISO 8601 UTC timestamp of the last update (e.g. `"2026-06-23T14:32:00Z"`). |
| `completed_steps` | integer | Count of steps with status `completed` or `skipped` in `step_tracker.json`. Must match exactly. |
| `next_step_number` | integer | Step number of the next pending step from `step_tracker.json`. Set to `null` when the task is done. |
| `next_step_id` | string | Step id (slug) of the next pending step. Set to `null` when the task is done. |

### Mandatory Sections

Four sections appear after the frontmatter in this fixed order:

```markdown
# Task Objective
<one sentence from task.json short_description>

---

## Step History

### Step N — <step-id>
<max 3 sentences>

---

## Cross-Step Decisions
* **<label>**: <one-line description with downstream impact>

---

## Next Step Notes
<3-5 sentences>
```

| Section | Heading level | Required | Description |
| --- | --- | --- | --- |
| Task Objective | `#` | yes | One sentence copied verbatim from `task.json` `short_description`. |
| Step History | `##` | yes (when steps completed ≥ 1) | One `### Step N — <step-id>` subsection per completed or skipped step, in order. |
| Cross-Step Decisions | `##` | yes (may be empty list) | Bullet list of decisions with downstream impact. |
| Next Step Notes | `##` | yes | 3-5 sentences for the next step-executor. Overwritten each step. |

* * *

## Content Rules

### Task Objective

Copy the `short_description` field from `task.json` verbatim. One sentence only. Do not paraphrase
or summarize.

### Step History

Each completed or skipped step gets one subsection:

```markdown
### Step 3 — planning
Designed a two-phase approach: offline calibration on 500-sample subset then full 40K run.
Selected A100-80G tier based on VRAM estimate from research-papers step.
Plan committed to `plan/plan.md` with cost estimate $42.
```

Rules:

* Maximum 3 sentences per entry.
* Focus: decision made, key output file created, any caveat for downstream steps.
* Written by the step-executor that completed the step, not retroactively edited.
* Include every completed and skipped step in order. Do not omit steps.
* For skipped steps: one sentence stating the step was skipped and the reason.

### Cross-Step Decisions

Only decisions that affect future steps belong here. Routine completions do not.

```markdown
## Cross-Step Decisions
* **GPU tier**: A100 80 GB, authorized by user during planning.
* **Dataset variant**: en-only subset (40K rows), confirmed in research-papers step.
* **Budget override**: $80 approved by user for GPU provisioning.
```

Do not add:

* Decisions whose effect is already complete (e.g., "created branch feat/xyz").
* Implementation details that future steps do not need.
* Anything already captured in plan files that the step-executor will read.

### Next Step Notes

Overwritten completely each step. Written by the step-executor that just finished, addressed to the
next step-executor.

```markdown
## Next Step Notes
The planning step produced `plan/plan.md` with a cost estimate of $42 and selected vLLM 0.19.1
on A100 80 GB. The dataset is already downloaded at `assets/dataset/en_only_40k/`. The next
step is setup-machines: provision one A100 VM per the machine spec in the plan. The user
authorized up to $80 for GPU provisioning. Run the smoke gate before starting warmup.
```

Rules:

* 3-5 sentences.
* Address the specific next step by name.
* Include any user authorizations or budget decisions relevant to the next step.
* Do not repeat information already in Cross-Step Decisions — refer to it instead.

* * *

## Lifecycle

### Creation

The coordinator creates a minimal `checkpoint.md` after step 1 completes (before step 2 starts):

```yaml
---
spec_version: "1"
task_id: "<task_id>"
updated_at: "<ISO8601 UTC timestamp>"
completed_steps: 1
next_step_number: 2
next_step_id: "<step-2-id>"
---
```

```markdown
# Task Objective
<short_description from task.json>

---

## Step History

### Step 1 — create-branch
Branch `task/<task_id>` created. Initial folder structure initialized in `tasks/<task_id>/`.
Step 1 is a mechanical setup step with no research output.

---

## Cross-Step Decisions

---

## Next Step Notes
Step 1 completed successfully. The task branch and folder are ready. Proceed to step 2 per
step_tracker.json.
```

Commit with message: `$TASK_ID [coordinator]: Initialize checkpoint.md after step 1`

### Updates (step ≥ 2)

Each step-executor updates `checkpoint.md` as the last action before calling poststep:

1. Append a `### Step N — <step-id>` entry to Step History.
2. Add any new downstream-impacting decisions to Cross-Step Decisions.
3. Overwrite the Next Step Notes section entirely.
4. Update all frontmatter fields: `updated_at`, `completed_steps`, `next_step_number`,
   `next_step_id`.
5. Run `uv run flowmark --inplace --nobackup tasks/$TASK_ID/checkpoint.md`.
6. Include `checkpoint.md` in the step commit.

### Resume of Pre-v25 Tasks

When a task has completed steps but no `checkpoint.md` (pre-v25 task), the coordinator synthesizes a
minimal checkpoint before spawning the next step-executor:

* Step History: one entry per completed step, body
  `"Synthesized for v25 resume — no detail available."`.
* Cross-Step Decisions: empty.
* Next Step Notes:
  `"Resuming pre-v25 task. Prior step details unavailable; read plan.md and step logs for context."`

Commit with message: `$TASK_ID [coordinator]: Synthesize checkpoint.md for v25 resume`

* * *

## Size Limit

`checkpoint.md` must not exceed 10 KB. If it approaches the limit, trim the oldest Step History
entries (not Cross-Step Decisions or Next Step Notes) to bring it under the limit. Trimmed entries
may be replaced with: `### Step N — <step-id>\nTrimmed to stay within 10 KB limit.`

* * *

## Verificator

`arf/scripts/verificators/verify_checkpoint.py` enforces the structural and consistency checks
listed below. It does not validate prose quality, section ordering, or content beyond what is
described in the table.

| Code | Severity | Check |
| --- | --- | --- |
| `CK-E001` | error | `checkpoint.md` missing when task has ≥ 1 completed step |
| `CK-E002` | error | YAML frontmatter missing, not parseable, or `spec_version` value not supported |
| `CK-E003` | error | Required frontmatter field missing (`spec_version`, `task_id`, `updated_at`, `completed_steps`, `next_step_number`, `next_step_id`) |
| `CK-E004` | error | `task_id` is null, wrong type, or does not match task folder name |
| `CK-E005` | error | `next_step_number` or `next_step_id` does not match the next pending step in `step_tracker.json` |
| `CK-E006` | error | `completed_steps` is null, wrong type, or count does not match `step_tracker.json` (completed + skipped) |
| `CK-E007` | error | Step History missing or malformed entry (`### Step N — <step-id>` required) for a completed or skipped step |
| `CK-W001` | warning | File size > 10 KB |
| `CK-W002` | warning | A Step History entry exceeds ~100 words |
| `CK-W003` | warning | `updated_at` is older than the latest completed step's `completed_at` in `step_tracker.json` |
| `CK-W004` | warning | Task Objective section missing or empty |

Run:

```bash
uv run python -m arf.scripts.verificators.verify_checkpoint <task_id>
```
