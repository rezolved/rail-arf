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
LESSON_CITATION_PATTERN = re.compile(r"\bLessons?\s+(\d+)")

# A citation may chain further numbers with "and", and a short parenthetical may sit
# between them: "Lessons 10 (persistent storage) and 11 (systemd lingering)". Matching
# only "<n> and <m>" silently drops the second number in that form, which is the exact
# blind spot this checker exists to prevent. The parenthetical is bounded and may not
# span lines, so the scan cannot run on into unrelated prose.
LESSON_CHAIN_PATTERN = re.compile(r"(?:\s*\([^)\n]{0,60}\))?\s+and\s+(\d+)\b")


def cited_lesson_numbers(*, text: str) -> list[str]:
    numbers: list[str] = []
    for citation in LESSON_CITATION_PATTERN.finditer(text):
        numbers.append(citation.group(1))
        position: int = citation.end()
        while (chained := LESSON_CHAIN_PATTERN.match(text, position)) is not None:
            numbers.append(chained.group(1))
            position = chained.end()
    return numbers


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
            for number in cited_lesson_numbers(text=text):
                if number not in defined:
                    rel: str = str(path.relative_to(REPO_ROOT))
                    dangling.append(f"{rel} cites Lesson {number}")

    assert len(dangling) == 0, (
        "these citations point at a lesson that does not exist in LESSONS.md or "
        "project/LESSONS.md:\n  " + "\n  ".join(sorted(set(dangling)))
    )


def test_citation_parser_reads_every_number_in_a_chained_citation() -> None:
    """A parenthetical between two chained numbers must not hide the second one.

    Every string below is a real citation form from this repo. The middle one is the
    case that a ``<n> and <m>`` pattern drops: ``LESSONS.md Lessons 10 (persistent
    storage) and 11 (systemd lingering)`` yields only ``10``, so a checker built on it
    would pass while Lesson 11 went undefined.
    """
    assert cited_lesson_numbers(text="see LESSONS.md Lesson 8 for the rationale") == ["8"]
    assert cited_lesson_numbers(text="LESSONS.md Lessons 10 and 11).") == ["10", "11"]
    assert cited_lesson_numbers(
        text="LESSONS.md Lessons 10 (persistent storage) and 11 (systemd lingering).",
    ) == ["10", "11"]


def test_citation_parser_does_not_swallow_unrelated_numbers() -> None:
    """The chain stops at the first thing that is not ``and <number>``.

    A scan that merely collected digits near the word "Lesson" would read a date or an
    adjacent clause as a lesson number and report a dangling citation that is not one —
    a checker that cries wolf gets switched off.
    """
    assert cited_lesson_numbers(
        text="Lessons 11 and 10 — a killed job and a lost adapter — and both are cheap",
    ) == ["11", "10"]
    assert cited_lesson_numbers(text="Lesson 8's fourth follow-up (2026-07-31) explains") == ["8"]
    assert cited_lesson_numbers(text="## Lessons Learned\n\n3 items") == []
    assert cited_lesson_numbers(text="Lesson 3 applies. Section 7 does not.") == ["3"]
