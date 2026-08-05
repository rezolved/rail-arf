# Contributing to rail-arf

rail-arf is Rezolve's canonical fork-base for research projects (latency, fine-tuning, RAG,
guardrails, etc.). It started as a fork of `GliteTech/glite-arf` but is developed independently —
there is no upstream tracking. Modify `arf/`, `meta/`, `LESSONS.md`, and the root `CLAUDE.md`
freely; these are Rezolve's now.

## Spawning a new Rezolve research project

```bash
gh repo fork rezolved/rail-arf --org rezolved --fork-name rail-arf-<project-slug>
```

Then run `/setup-project` in Claude Code to populate `project/description.md`,
`project/budget.json`, and the project's `meta/` entries.

## Modifying the framework

Modifications to `arf/`, `meta/` (generic types only), `LESSONS.md`, or the root `CLAUDE.md` go on
`main` directly. No `tasks/tXXXX_*` folder — those are reserved for project work, per key rule 0 in
`CLAUDE.md`. Open a PR if the change is non-trivial. Always increment the version of any skill or
specification you change (plain integer, +1 per change — see
`arf/styleguide/agent_instructions_styleguide.md`).

## Adding a lesson

Lessons live in two files. `LESSONS.md` holds portable ones — those whose enforcement ships in this
fork-base — and is copied into forks verbatim. A fork's own lessons go in its `project/LESSONS.md`,
which never comes back here. Decide which applies before writing, because a lesson in the wrong file
either gets lost on the next port or arrives in a fork that has nothing to enforce it.

Follow the procedure at the bottom of whichever file applies: add a `## Lesson N` entry with *what
went wrong*, *why*, *mitigation*, and wire the mitigation as a default in the relevant skill, asset
spec, or verificator. A lesson without a corresponding code default is a complaint, not a lesson —
if you record one anyway, say so explicitly rather than letting it read as a guarantee.

**Lesson numbers are globally unique across both files and are never reused or renumbered.** They
are cited from skills, specifications, and scripts here, and from immutable completed task folders
in every fork. Take the next unused number across both files; a gap is a lesson that lives in the
other one. `arf/tests/test_lesson_references_resolve.py` fails if a citation in `arf/` or `meta/`
points at a lesson that does not exist.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pre-commit install
python3 doctor.py
```

## Quality checks

Before opening a pull request, run every check locally:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy .
uv run pytest
```

Pull requests with failing checks will not be merged.

## Markdown files

Edited markdown files must be normalized with Flowmark at width 100:

```bash
uv run flowmark --inplace --nobackup path/to/file.md
```

See `arf/styleguide/markdown_styleguide.md` for the full rules.

## Style guides

All code and documentation must follow the project style guides:

* `arf/styleguide/python_styleguide.md` — Python code
* `arf/styleguide/markdown_styleguide.md` — Markdown files
* `arf/styleguide/agent_instructions_styleguide.md` — skills and agent instructions

The style guides are enforced by `arf/skills/check-python-style`, `arf/skills/check-markdown-style`,
and `arf/skills/check-skill`. Run the relevant checker skill on any file you touch.

## Proposing a new specification, verificator, aggregator, or skill

1. Open an issue first describing the proposal and the problem it solves. This saves wasted work on
   proposals that do not fit the framework scope.
2. Write the specification first, not the implementation. Place it under `arf/specifications/` or
   `meta/asset_types/<name>/specification.md` with a `**Version**: 1` header.
3. Write tests before the implementation. See `arf/tests/` for the pattern.
4. Implement the verificator or aggregator under `arf/scripts/verificators/` or
   `arf/scripts/aggregators/`.
5. Add a skill under `arf/skills/<skill-slug>/SKILL.md` if the change introduces a new workflow.
   Symlink it into `.claude/skills/<skill-slug>` and `.codex/skills/<skill-slug>` using relative
   paths.
6. Update `arf/docs/reference/` so the new component is discoverable.

## Versioning and releases

Specifications and skills carry plain-integer version numbers (`**Version**: N`). Increment by one
for every backwards-incompatible change. Files produced under a spec carry a matching `spec_version`
string field.

There is no fixed release cadence; pin commit SHAs in downstream projects if you need
reproducibility.
