# rail-arf — Rezolve's Canonical ARF Fork

Template repository for starting a Rezolve research project (latency, fine-tuning, RAG, guardrails,
etc.) using the Glite Autonomous Research Framework. Fork this repo and replace this section with
your project-specific description.

This started as a fork of the Glite ARF template but is developed independently by Rezolve. On top
of the base framework it adds:

* An Azure ML SSH-VM provisioner (`arf/scripts/utils/azure_ml_vm.py`) wired into the
  `setup-remote-machine` skill — Azure ML is the default GPU provider for Rezolve projects.
* A statistically-significant paired-bootstrap library (`arf/scripts/stats/bootstrap_compare/`) with
  locked seeds, used for any cross-condition comparison.
* A warmup-protocol shape (`arf/scripts/protocols/warmup_runner/`) — engine-agnostic constants and
  spec for the warmup-N + measured-M latency-benchmark pattern.
* A `LESSONS.md` at the repo root capturing hard-won lessons from prior Rezolve research projects,
  encoded as defaults in skills and verificators. It holds only portable lessons — ones whose
  enforcement ships here. A fork records its own in `project/LESSONS.md`, which never ports back.

## Commands

```bash
# Setup
# New fork onboarding: run /setup-project in Claude Code or $setup-project in Codex
uv sync                                            # Install deps
uv run pre-commit install                          # Activate git hooks
python3 doctor.py                                  # Validate environment

# Development
uv run python -u <script.py>                                       # Run a script
uv run python -m arf.scripts.utils.run_with_logs --task-id <id> -- <cmd>   # ARF logging

# Quality
uv run flowmark --inplace --nobackup <path.md>      # Format markdown
uv run ruff check --fix . && uv run ruff format .   # Lint and format Python
uv run mypy .                                        # Type check
uv run pytest                                        # Run framework tests in arf/tests
```

## Key References

* Project description and goals: create `project/description.md` in your fork
* New project onboarding: @arf/skills/setup-project/SKILL.md
* ARF architecture and glossary: @arf/README.md
* Portable ARF lessons accumulated over prior projects: @LESSONS.md
* Python style guide: @arf/styleguide/python_styleguide.md
* Markdown style guide: @arf/styleguide/markdown_styleguide.md
* Agent instructions style guide: @arf/styleguide/agent_instructions_styleguide.md
* Paper asset specification: @meta/asset_types/paper/specification.md
* Aggregators reference: @arf/docs/reference/aggregators.md

## Rezolve conventions

* Always write **brainpowa** in lowercase — never "Brainpowa" or "BrainPowa".
* Use the `gh` CLI for GitHub operations (PR reads, diffs, status checks). Never paste credentials
  into the repo.
* Never add a "Generated with Claude Code" promo line to commit messages or PR descriptions.
* The default GPU provider is **Azure ML** (configured via `project/azure_vm.json`). vast.ai is
  supported as a fallback if a project declares it in `available_services`.

## Key Rules

0. Framework / infrastructure / specification / skill / verificator / aggregator / materializer
   changes in `arf/`, generic `meta/`, and generic boilerplate are not task work. Do not create a
   `tasks/tXXXX_*` folder for such changes.
1. All CLI tool calls MUST be wrapped in `arf/scripts/utils/run_with_logs.py`.
2. One task = one folder = one branch = one PR.
3. **NEVER** modify files outside the task folder. Only top-level tooling files may change:
   `pyproject.toml`, `uv.lock`, `ruff.toml`, `.gitignore`.
4. Each task stage and each action is a separate well-described commit.
5. Nothing in a completed task folder may be changed; use the corrections mechanism in later tasks.
6. Run `uv run flowmark --inplace --nobackup <changed.md>` on edited markdown files, then run
   `uv run ruff check --fix . && uv run ruff format . && uv run mypy .` before commit.
7. Framework tests live in `arf/tests/`. Task-specific tests live in
   `tasks/$TASK_ID/code/test_*.py`. Do not create or use a top-level `tests/` directory.
8. Full data normalization; no duplication across task folders.
9. **Always use aggregators to enumerate cross-task data.** Never walk `tasks/` with
   Glob/Grep/find/Explore to list tasks, papers, suggestions, answers, datasets, libraries, models,
   predictions, costs, metrics, or metric results. Aggregators in `arf/scripts/aggregators/` apply
   the corrections overlay; raw filesystem walks silently miss corrections and produce stale
   answers. See `arf/docs/reference/aggregators.md` for the full list and flags.
10. Read `LESSONS.md` — and a fork's `project/LESSONS.md` if it has one — before planning a task
    that involves latency benchmarks, GPU provisioning, quantization, or paired-bootstrap analysis.
    Each lesson lists the mitigation already wired into the framework and the verificator that
    enforces it. Lesson numbers are globally unique across the two files and are never reused or
    renumbered.

## Task Workflow

* Tasks live in `tasks/tXXXX_slug/` (t prefix + 4-digit ID + underscore slug).
* Each task runs in its own git worktree on branch `task/<task_id>`.
* Multiple tasks can execute in parallel (each in a separate worktree).
* New tasks branch: `new_tasks/<first_index>-<last_index>`.
* Mandatory stages: research -> planning -> implementation -> analysis -> reporting.
* Every step must be logged in `logs/`; verificators enforce this.
* Aggregators collect data across tasks AND apply corrections overlays — use them instead of walking
  `tasks/` directly (see rule 9).
* Format specs for task documents: `arf/specifications/`.

## Provenance

rail-arf started as a fork of `GliteTech/glite-arf` but is developed independently. There is no
`upstream` remote and we do not track upstream changes. Modify `arf/` (skills, verificators,
aggregators, specs) freely — those are Rezolve's now.
