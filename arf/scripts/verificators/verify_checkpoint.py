"""Verificator for task checkpoint files.

Checks checkpoint.md against the checkpoint specification
(arf/specifications/checkpoint_specification.md). Cross-references
step_tracker.json to validate step counts and next-step fields.

Usage:
    uv run python -m arf.scripts.verificators.verify_checkpoint <task_id>

Exit codes:
    0 — no errors (warnings may be present)
    1 — one or more errors found
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arf.scripts.verificators.common.frontmatter import (
    FrontmatterResult,
    extract_frontmatter_and_body,
    parse_frontmatter,
)
from arf.scripts.verificators.common.markdown_sections import (
    MarkdownSection,
    count_words,
    extract_sections,
)
from arf.scripts.verificators.common.paths import (
    checkpoint_path,
    step_tracker_path,
)
from arf.scripts.verificators.common.reporting import (
    exit_code_for_result,
    print_verification_result,
)
from arf.scripts.verificators.common.types import (
    Diagnostic,
    DiagnosticCode,
    Severity,
    VerificationResult,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PREFIX: str = "CK"

FRONTMATTER_FIELD_SPEC_VERSION: str = "spec_version"
FRONTMATTER_FIELD_TASK_ID: str = "task_id"
FRONTMATTER_FIELD_UPDATED_AT: str = "updated_at"
FRONTMATTER_FIELD_COMPLETED_STEPS: str = "completed_steps"
FRONTMATTER_FIELD_NEXT_STEP_NUMBER: str = "next_step_number"
FRONTMATTER_FIELD_NEXT_STEP_ID: str = "next_step_id"

REQUIRED_FRONTMATTER_FIELDS: list[str] = [
    FRONTMATTER_FIELD_SPEC_VERSION,
    FRONTMATTER_FIELD_TASK_ID,
    FRONTMATTER_FIELD_UPDATED_AT,
    FRONTMATTER_FIELD_COMPLETED_STEPS,
    FRONTMATTER_FIELD_NEXT_STEP_NUMBER,
    FRONTMATTER_FIELD_NEXT_STEP_ID,
]

SECTION_TASK_OBJECTIVE: str = "Task Objective"
SECTION_STEP_HISTORY: str = "Step History"
SECTION_CROSS_STEP_DECISIONS: str = "Cross-Step Decisions"
SECTION_NEXT_STEP_NOTES: str = "Next Step Notes"

STEP_TRACKER_FIELD_STEPS: str = "steps"
STEP_TRACKER_FIELD_STEP: str = "step"
STEP_TRACKER_FIELD_NAME: str = "name"
STEP_TRACKER_FIELD_STATUS: str = "status"
STEP_TRACKER_FIELD_COMPLETED_AT: str = "completed_at"

STATUS_COMPLETED: str = "completed"
STATUS_SKIPPED: str = "skipped"
STATUS_PENDING: str = "pending"
STATUS_IN_PROGRESS: str = "in_progress"

CHECKPOINT_SPEC_VERSION: str = "1"

FILE_SIZE_LIMIT_BYTES: int = 10 * 1024  # 10 KB
STEP_HISTORY_WORD_LIMIT: int = 100

# ---------------------------------------------------------------------------
# Diagnostic codes
# ---------------------------------------------------------------------------

CODE_CK_E001: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=1,
)
CODE_CK_E002: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=2,
)
CODE_CK_E003: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=3,
)
CODE_CK_E004: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=4,
)
CODE_CK_E005: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=5,
)
CODE_CK_E006: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=6,
)
CODE_CK_E007: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.ERROR,
    number=7,
)
CODE_CK_W001: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=1,
)
CODE_CK_W002: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=2,
)
CODE_CK_W003: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=3,
)
CODE_CK_W004: DiagnosticCode = DiagnosticCode(
    prefix=_PREFIX,
    severity=Severity.WARNING,
    number=4,
)

# ---------------------------------------------------------------------------
# Step-tracker helpers
# ---------------------------------------------------------------------------


def _parse_iso8601(*, value: str) -> datetime:
    normalized: str = value.replace("Z", "+00:00")
    parsed: datetime = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _load_step_tracker(*, task_id: str) -> list[dict[str, Any]]:
    tracker_path: Path = step_tracker_path(task_id=task_id)
    if not tracker_path.exists():
        return []
    try:
        raw: str = tracker_path.read_text(encoding="utf-8")
        data: object = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    steps: object = data.get(STEP_TRACKER_FIELD_STEPS)
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, dict)]


def _completed_steps(*, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        s for s in steps if s.get(STEP_TRACKER_FIELD_STATUS) in (STATUS_COMPLETED, STATUS_SKIPPED)
    ]


def _next_pending_step(*, steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    for step in steps:
        if step.get(STEP_TRACKER_FIELD_STATUS) == STATUS_PENDING:
            return step
    return None


def _promote_in_progress_step(
    *,
    steps: list[dict[str, Any]],
    step_id: str,
) -> list[dict[str, Any]]:
    """Return steps with the given in_progress step treated as completed.

    Called from poststep before step_tracker.json is updated, so the step-executor's
    checkpoint.md (which already counts this step as completed) can be verified without
    CK-E006/CK-E007 false positives.
    """
    result: list[dict[str, Any]] = []
    for step in steps:
        if (
            step.get(STEP_TRACKER_FIELD_NAME) == step_id
            and step.get(STEP_TRACKER_FIELD_STATUS) == STATUS_IN_PROGRESS
        ):
            result.append({**step, STEP_TRACKER_FIELD_STATUS: STATUS_COMPLETED})
        else:
            result.append(step)
    return result


def _latest_completed_at(*, completed: list[dict[str, Any]]) -> datetime | None:
    latest: datetime | None = None
    for step in completed:
        raw_ts: object = step.get(STEP_TRACKER_FIELD_COMPLETED_AT)
        if not isinstance(raw_ts, str):
            continue
        try:
            ts: datetime = _parse_iso8601(value=raw_ts)
        except ValueError:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------


def _check_existence(
    *,
    task_id: str,
    file_path: Path,
    steps: list[dict[str, Any]],
) -> list[Diagnostic]:
    completed: list[dict[str, Any]] = _completed_steps(steps=steps)
    if len(completed) == 0:
        return []
    if file_path.exists():
        return []
    return [
        Diagnostic(
            code=CODE_CK_E001,
            message=(
                f"checkpoint.md missing for task '{task_id}' which has"
                f" {len(completed)} completed step(s)"
            ),
            file_path=file_path,
        )
    ]


def _check_frontmatter(
    *,
    content: str,
    file_path: Path,
) -> tuple[list[Diagnostic], FrontmatterResult | None, dict[str, Any] | None]:
    fm_result: FrontmatterResult | None = extract_frontmatter_and_body(content=content)
    if fm_result is None:
        return (
            [
                Diagnostic(
                    code=CODE_CK_E002,
                    message="YAML frontmatter missing or does not start at line 1",
                    file_path=file_path,
                )
            ],
            None,
            None,
        )

    parsed: dict[str, Any] | None = parse_frontmatter(raw_yaml=fm_result.raw_yaml)
    if parsed is None:
        return (
            [
                Diagnostic(
                    code=CODE_CK_E002,
                    message="YAML frontmatter is not parseable",
                    file_path=file_path,
                    detail=fm_result.raw_yaml[:200],
                )
            ],
            fm_result,
            None,
        )

    diagnostics: list[Diagnostic] = []
    missing: list[str] = [f for f in REQUIRED_FRONTMATTER_FIELDS if f not in parsed]
    for field_name in missing:
        diagnostics.append(
            Diagnostic(
                code=CODE_CK_E003,
                message=f"Required frontmatter field '{field_name}' is missing",
                file_path=file_path,
            )
        )

    version: object = parsed.get(FRONTMATTER_FIELD_SPEC_VERSION)
    if version is not None and str(version) != CHECKPOINT_SPEC_VERSION:
        diagnostics.append(
            Diagnostic(
                code=CODE_CK_E002,
                message=(
                    f"spec_version {version!r} is not supported;"
                    f" expected {CHECKPOINT_SPEC_VERSION!r}"
                ),
                file_path=file_path,
            )
        )

    return diagnostics, fm_result, parsed


def _check_task_id(
    *,
    task_id: str,
    frontmatter: dict[str, Any],
    file_path: Path,
) -> list[Diagnostic]:
    fm_task_id: object = frontmatter.get(FRONTMATTER_FIELD_TASK_ID)
    if fm_task_id is None:
        if FRONTMATTER_FIELD_TASK_ID in frontmatter:
            return [
                Diagnostic(
                    code=CODE_CK_E004,
                    message="task_id is null; expected a string matching the task folder name",
                    file_path=file_path,
                )
            ]
        return []  # Missing — CK-E003 handles it
    if not isinstance(fm_task_id, str):
        return [
            Diagnostic(
                code=CODE_CK_E004,
                message=(
                    f"task_id has wrong type:"
                    f" expected str, got {type(fm_task_id).__name__}"
                ),
                file_path=file_path,
            )
        ]
    if fm_task_id == task_id:
        return []
    return [
        Diagnostic(
            code=CODE_CK_E004,
            message=(
                f"frontmatter task_id '{fm_task_id}' does not match task folder name '{task_id}'"
            ),
            file_path=file_path,
        )
    ]


def _check_next_step_fields(
    *,
    frontmatter: dict[str, Any],
    steps: list[dict[str, Any]],
    file_path: Path,
) -> list[Diagnostic]:
    fm_next_num: object = frontmatter.get(FRONTMATTER_FIELD_NEXT_STEP_NUMBER)
    fm_next_id: object = frontmatter.get(FRONTMATTER_FIELD_NEXT_STEP_ID)
    next_num_present: bool = FRONTMATTER_FIELD_NEXT_STEP_NUMBER in frontmatter
    next_id_present: bool = FRONTMATTER_FIELD_NEXT_STEP_ID in frontmatter
    next_pending: dict[str, Any] | None = _next_pending_step(steps=steps)

    diagnostics: list[Diagnostic] = []

    # Validate next_step_number — skip when field is absent (CK-E003 already fires)
    if next_num_present:
        if fm_next_num is None and next_pending is not None:
            tracker_next: object = next_pending.get(STEP_TRACKER_FIELD_STEP)
            diagnostics.append(
                Diagnostic(
                    code=CODE_CK_E005,
                    message=f"next_step_number is null but step {tracker_next} is still pending",
                    file_path=file_path,
                )
            )
        elif fm_next_num is not None and next_pending is None:
            diagnostics.append(
                Diagnostic(
                    code=CODE_CK_E005,
                    message=f"next_step_number is {fm_next_num!r} but no pending steps remain",
                    file_path=file_path,
                )
            )
        elif fm_next_num is not None and next_pending is not None:
            if not isinstance(fm_next_num, int):
                diagnostics.append(
                    Diagnostic(
                        code=CODE_CK_E005,
                        message=(
                            f"next_step_number has wrong type:"
                            f" expected int, got {type(fm_next_num).__name__}"
                        ),
                        file_path=file_path,
                    )
                )
            else:
                tracker_next_num: object = next_pending.get(STEP_TRACKER_FIELD_STEP)
                if isinstance(tracker_next_num, int) and fm_next_num != tracker_next_num:
                    diagnostics.append(
                        Diagnostic(
                            code=CODE_CK_E005,
                            message=(
                                f"next_step_number {fm_next_num} does not match"
                                f" step_tracker.json next pending step {tracker_next_num}"
                            ),
                            file_path=file_path,
                        )
                    )

    # Validate next_step_id — skip when field is absent (CK-E003 already fires)
    if next_id_present:
        if fm_next_id is None and next_pending is not None:
            tracker_next_id: object = next_pending.get(STEP_TRACKER_FIELD_NAME)
            diagnostics.append(
                Diagnostic(
                    code=CODE_CK_E005,
                    message=f"next_step_id is null but step '{tracker_next_id}' is still pending",
                    file_path=file_path,
                )
            )
        elif fm_next_id is not None and next_pending is None:
            diagnostics.append(
                Diagnostic(
                    code=CODE_CK_E005,
                    message=f"next_step_id is {fm_next_id!r} but no pending steps remain",
                    file_path=file_path,
                )
            )
        elif fm_next_id is not None and next_pending is not None:
            tracker_next_id_str: object = next_pending.get(STEP_TRACKER_FIELD_NAME)
            if fm_next_id != tracker_next_id_str:
                diagnostics.append(
                    Diagnostic(
                        code=CODE_CK_E005,
                        message=(
                            f"next_step_id {fm_next_id!r} does not match"
                            f" step_tracker.json next pending step name {tracker_next_id_str!r}"
                        ),
                        file_path=file_path,
                    )
                )

    return diagnostics


def _check_completed_steps_count(
    *,
    frontmatter: dict[str, Any],
    steps: list[dict[str, Any]],
    file_path: Path,
) -> list[Diagnostic]:
    fm_count: object = frontmatter.get(FRONTMATTER_FIELD_COMPLETED_STEPS)
    if fm_count is None:
        if FRONTMATTER_FIELD_COMPLETED_STEPS in frontmatter:
            return [
                Diagnostic(
                    code=CODE_CK_E006,
                    message="completed_steps is null; expected an integer",
                    file_path=file_path,
                )
            ]
        return []
    if not isinstance(fm_count, int):
        return [
            Diagnostic(
                code=CODE_CK_E006,
                message=(
                    f"completed_steps has wrong type: expected int, got {type(fm_count).__name__}"
                ),
                file_path=file_path,
            )
        ]
    tracker_count: int = len(_completed_steps(steps=steps))
    if fm_count == tracker_count:
        return []
    return [
        Diagnostic(
            code=CODE_CK_E006,
            message=(
                f"completed_steps {fm_count} does not match"
                f" step_tracker.json count of completed/skipped steps {tracker_count}"
            ),
            file_path=file_path,
        )
    ]


def _parse_step_history_heading(*, heading: str) -> tuple[int, str] | None:
    """Return (step_number, step_id) from 'Step 3 — planning', or None if malformed."""
    match: re.Match[str] | None = re.match(r"^Step\s+(\d+)\s+—\s+(\S+)$", heading.strip())
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def _check_step_history(
    *,
    body: str,
    steps: list[dict[str, Any]],
    file_path: Path,
) -> list[Diagnostic]:
    completed: list[dict[str, Any]] = _completed_steps(steps=steps)
    if len(completed) == 0:
        return []

    top_sections: list[MarkdownSection] = extract_sections(body=body, level=2)
    history_body: str = ""
    for section in top_sections:
        if section.heading.strip() == SECTION_STEP_HISTORY:
            history_body = section.content
            break

    history_entries: list[MarkdownSection] = extract_sections(
        body=history_body,
        level=3,
    )
    # Map step_number → step_id from headings that match "Step N — step-id" exactly
    documented: dict[int, str] = {}
    for entry in history_entries:
        parsed: tuple[int, str] | None = _parse_step_history_heading(heading=entry.heading)
        if parsed is not None:
            documented[parsed[0]] = parsed[1]

    diagnostics: list[Diagnostic] = []
    for step in completed:
        step_num_obj: object = step.get(STEP_TRACKER_FIELD_STEP)
        if not isinstance(step_num_obj, int):
            continue
        step_name: object = step.get(STEP_TRACKER_FIELD_NAME, "unknown")
        if step_num_obj not in documented:
            diagnostics.append(
                Diagnostic(
                    code=CODE_CK_E007,
                    message=(
                        f"Step History missing entry for completed or skipped step"
                        f" {step_num_obj} ({step_name})"
                    ),
                    file_path=file_path,
                )
            )
        elif documented[step_num_obj] != str(step_name):
            diagnostics.append(
                Diagnostic(
                    code=CODE_CK_E007,
                    message=(
                        f"Step History entry for step {step_num_obj} has step_id"
                        f" {documented[step_num_obj]!r} but step_tracker.json"
                        f" name is {step_name!r}"
                    ),
                    file_path=file_path,
                )
            )

    return diagnostics


def _check_file_size(
    *,
    file_path: Path,
    content: str,
) -> list[Diagnostic]:
    size: int = len(content.encode("utf-8"))
    if size <= FILE_SIZE_LIMIT_BYTES:
        return []
    return [
        Diagnostic(
            code=CODE_CK_W001,
            message=(f"checkpoint.md is {size} bytes, exceeds {FILE_SIZE_LIMIT_BYTES} byte limit"),
            file_path=file_path,
            detail="Trim oldest Step History entries to bring under 10 KB.",
        )
    ]


def _check_step_history_entry_lengths(
    *,
    body: str,
    file_path: Path,
) -> list[Diagnostic]:
    top_sections: list[MarkdownSection] = extract_sections(body=body, level=2)
    history_body: str = ""
    for section in top_sections:
        if section.heading.strip() == SECTION_STEP_HISTORY:
            history_body = section.content
            break
    if len(history_body) == 0:
        return []

    history_entries: list[MarkdownSection] = extract_sections(
        body=history_body,
        level=3,
    )
    diagnostics: list[Diagnostic] = []
    for entry in history_entries:
        word_count: int = count_words(text=entry.content)
        if word_count > STEP_HISTORY_WORD_LIMIT:
            diagnostics.append(
                Diagnostic(
                    code=CODE_CK_W002,
                    message=(
                        f"Step History entry '{entry.heading}' has {word_count} words"
                        f" (limit: ~{STEP_HISTORY_WORD_LIMIT})"
                    ),
                    file_path=file_path,
                )
            )
    return diagnostics


def _check_updated_at(
    *,
    frontmatter: dict[str, Any],
    steps: list[dict[str, Any]],
    file_path: Path,
) -> list[Diagnostic]:
    updated_at_raw: object = frontmatter.get(FRONTMATTER_FIELD_UPDATED_AT)
    if updated_at_raw is None:
        if FRONTMATTER_FIELD_UPDATED_AT in frontmatter:
            return [
                Diagnostic(
                    code=CODE_CK_E003,
                    message="updated_at is null; expected an ISO 8601 UTC timestamp string",
                    file_path=file_path,
                )
            ]
        return []  # Missing — CK-E003 handles it from _check_required_fields
    if not isinstance(updated_at_raw, str):
        return [
            Diagnostic(
                code=CODE_CK_E003,
                message=(
                    f"updated_at has wrong type: expected str,"
                    f" got {type(updated_at_raw).__name__}"
                ),
                file_path=file_path,
            )
        ]
    try:
        updated_at: datetime = _parse_iso8601(value=updated_at_raw)
    except ValueError:
        return [
            Diagnostic(
                code=CODE_CK_E003,
                message=f"updated_at is not a valid ISO 8601 UTC timestamp: {updated_at_raw!r}",
                file_path=file_path,
            )
        ]

    completed: list[dict[str, Any]] = _completed_steps(steps=steps)
    latest: datetime | None = _latest_completed_at(completed=completed)
    if latest is None:
        return []
    if updated_at >= latest:
        return []
    return [
        Diagnostic(
            code=CODE_CK_W003,
            message=(
                f"updated_at ({updated_at_raw}) is older than the latest completed"
                f" step's completed_at ({latest.isoformat()})"
            ),
            file_path=file_path,
        )
    ]


def _check_task_objective(
    *,
    body: str,
    file_path: Path,
) -> list[Diagnostic]:
    top_sections: list[MarkdownSection] = extract_sections(body=body, level=1)
    for section in top_sections:
        if section.heading.strip() == SECTION_TASK_OBJECTIVE:
            # section.content spans until the next level-1 heading, so it includes all
            # level-2 sub-sections (## Step History, etc.). Strip everything from the
            # first sub-heading onward to get just the objective text.
            objective_text: str = re.split(r"\n##", section.content, maxsplit=1)[0]
            if re.search(r"\w", objective_text) is not None:
                return []
            return [
                Diagnostic(
                    code=CODE_CK_W004,
                    message="Task Objective section is empty",
                    file_path=file_path,
                )
            ]
    return [
        Diagnostic(
            code=CODE_CK_W004,
            message="Task Objective section ('# Task Objective') is missing",
            file_path=file_path,
        )
    ]


# ---------------------------------------------------------------------------
# Main verification function
# ---------------------------------------------------------------------------


def verify_checkpoint(
    *,
    task_id: str,
    current_step_id: str | None = None,
) -> VerificationResult:
    file_path: Path = checkpoint_path(task_id=task_id)
    diagnostics: list[Diagnostic] = []

    steps: list[dict[str, Any]] = _load_step_tracker(task_id=task_id)

    # When called from poststep, the current step is still in_progress in step_tracker.json
    # even though checkpoint.md already counts it as completed. Promote it here so
    # CK-E006/CK-E007 don't false-fire.
    if current_step_id is not None:
        steps = _promote_in_progress_step(steps=steps, step_id=current_step_id)

    # CK-E001: missing checkpoint
    existence_errors: list[Diagnostic] = _check_existence(
        task_id=task_id,
        file_path=file_path,
        steps=steps,
    )
    if len(existence_errors) > 0:
        diagnostics.extend(existence_errors)
        return VerificationResult(file_path=file_path, diagnostics=diagnostics)

    if not file_path.exists():
        return VerificationResult(file_path=file_path, diagnostics=diagnostics)

    try:
        content: str = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        diagnostics.append(
            Diagnostic(
                code=CODE_CK_E002,
                message=f"Cannot read checkpoint.md: {exc}",
                file_path=file_path,
            )
        )
        return VerificationResult(file_path=file_path, diagnostics=diagnostics)

    # CK-W001: file size
    diagnostics.extend(_check_file_size(file_path=file_path, content=content))

    # CK-E002, CK-E003: frontmatter
    fm_errors, fm_result, frontmatter = _check_frontmatter(
        content=content,
        file_path=file_path,
    )
    diagnostics.extend(fm_errors)

    if frontmatter is not None:
        # CK-E004: task_id mismatch
        diagnostics.extend(
            _check_task_id(
                task_id=task_id,
                frontmatter=frontmatter,
                file_path=file_path,
            )
        )

        # CK-E005: next_step_number / next_step_id mismatches
        diagnostics.extend(
            _check_next_step_fields(
                frontmatter=frontmatter,
                steps=steps,
                file_path=file_path,
            )
        )

        # CK-E006: completed_steps count mismatch
        diagnostics.extend(
            _check_completed_steps_count(
                frontmatter=frontmatter,
                steps=steps,
                file_path=file_path,
            )
        )

        # CK-W003: updated_at staleness
        diagnostics.extend(
            _check_updated_at(
                frontmatter=frontmatter,
                steps=steps,
                file_path=file_path,
            )
        )

    if fm_result is not None:
        body: str = fm_result.body

        # CK-E007: missing Step History entries
        diagnostics.extend(
            _check_step_history(
                body=body,
                steps=steps,
                file_path=file_path,
            )
        )

        # CK-W002: oversized Step History entries
        diagnostics.extend(
            _check_step_history_entry_lengths(
                body=body,
                file_path=file_path,
            )
        )

        # CK-W004: Task Objective section
        diagnostics.extend(
            _check_task_objective(
                body=body,
                file_path=file_path,
            )
        )

    return VerificationResult(file_path=file_path, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Verify checkpoint.md against the checkpoint specification.",
    )
    parser.add_argument("task_id", help="Task ID (e.g. t0007_build_model_qwen35_moe)")
    parser.add_argument(
        "--current-step-id",
        dest="current_step_id",
        default=None,
        help=(
            "step_id of the in_progress step being completed by poststep."
            " Treats that step as completed so CK-E006/CK-E007 do not false-fire"
            " before step_tracker.json is updated."
        ),
    )
    args: argparse.Namespace = parser.parse_args()

    result: VerificationResult = verify_checkpoint(
        task_id=args.task_id,
        current_step_id=args.current_step_id,
    )
    print_verification_result(result=result)
    sys.exit(exit_code_for_result(result=result))


if __name__ == "__main__":
    main()
