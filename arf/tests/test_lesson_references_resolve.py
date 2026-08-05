"""Pin every ``Lesson N`` cited from framework code to a lesson that exists.

This test is not trivial, do not delete it.

Lessons are split across two files — portable ones in ``LESSONS.md``, project ones in
``project/LESSONS.md`` — and the numbers are cited from skills, specifications,
verificators, and scripts. Nothing in the type system or the linters connects a citation
to its target. Delete or renumber a lesson and every citation keeps rendering perfectly
while pointing at nothing, or worse, at a different lesson.

That is the same shape as the ``actual_status`` incident (``LESSONS.md`` Lesson 8) — a
consumer reading something no producer provides — and the same shape as the drift that
let ``heartbeat`` stamp a spec version two behind its specification. This is the cheap
standing check that citation and target still agree.

Only ``arf/`` and ``meta/`` are scanned. ``tasks/`` is immutable (``CLAUDE.md`` rule 5)
and ``overview/`` is generated from it, so both legitimately carry citations frozen at
whatever the numbering was when they were written.
"""

import re

from arf.scripts.verificators.common.paths import REPO_ROOT

ARF_LESSONS_PATH = REPO_ROOT / "LESSONS.md"
PROJECT_LESSONS_PATH = REPO_ROOT / "project" / "LESSONS.md"
SCANNED_DIRECTORIES: list[str] = ["arf", "meta"]
SCANNED_SUFFIXES: tuple[str, ...] = (".py", ".md", ".sh")

LESSON_HEADING_PATTERN = re.compile(r"^## Lesson (\d+):", re.MULTILINE)
LESSON_CITATION_PATTERN = re.compile(r"\bLessons?\s+(\d+)(?:\s+and\s+(\d+))?")


def _defined_lesson_numbers() -> set[str]:
    numbers: set[str] = set()
    for path in (ARF_LESSONS_PATH, PROJECT_LESSONS_PATH):
        if path.exists():
            numbers.update(LESSON_HEADING_PATTERN.findall(path.read_text(encoding="utf-8")))
    return numbers


def test_lesson_numbers_are_unique_across_both_files() -> None:
    """A number must mean one thing. Two files, one number space."""
    arf_numbers: list[str] = LESSON_HEADING_PATTERN.findall(
        ARF_LESSONS_PATH.read_text(encoding="utf-8"),
    )
    project_numbers: list[str] = (
        LESSON_HEADING_PATTERN.findall(PROJECT_LESSONS_PATH.read_text(encoding="utf-8"))
        if PROJECT_LESSONS_PATH.exists()
        else []
    )
    assert len(arf_numbers) == len(set(arf_numbers)), f"duplicate numbers in {ARF_LESSONS_PATH}"
    assert len(project_numbers) == len(set(project_numbers)), (
        f"duplicate numbers in {PROJECT_LESSONS_PATH}"
    )
    collisions: set[str] = set(arf_numbers) & set(project_numbers)
    assert len(collisions) == 0, (
        f"lesson numbers {sorted(collisions)} are defined in both LESSONS.md and "
        "project/LESSONS.md. Numbers are globally unique across both files."
    )


def test_every_cited_lesson_exists() -> None:
    defined: set[str] = _defined_lesson_numbers()
    assert len(defined) > 0, "at least one lesson is defined"

    dangling: list[str] = []
    for directory in SCANNED_DIRECTORIES:
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            text: str = path.read_text(encoding="utf-8", errors="replace")
            for first, second in LESSON_CITATION_PATTERN.findall(text):
                for number in (first, second):
                    if number != "" and number not in defined:
                        rel: str = str(path.relative_to(REPO_ROOT))
                        dangling.append(f"{rel} cites Lesson {number}")

    assert len(dangling) == 0, (
        "these citations point at a lesson that does not exist in LESSONS.md or "
        "project/LESSONS.md:\n  " + "\n  ".join(sorted(set(dangling)))
    )
