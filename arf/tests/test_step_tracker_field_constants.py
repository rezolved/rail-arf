"""Pin the two spellings of the ``step_tracker.json`` field names to each other.

This test is not trivial, do not delete it.

``prestep`` and ``heartbeat`` each define their own constants for the SAME tracker
fields, under two different naming conventions (``prestep.FIELD_STARTED_AT`` versus
``heartbeat.STARTED_AT_FIELD``). Nothing in the type system or the linters connects
them. If one side is renamed, ``finalize_step_liveness`` stops finding the timestamp
``prestep`` wrote, ``actual_duration_seconds`` goes ``null`` for every step, and nothing
raises: the failure is silent and looks like missing data rather than a bug.

That is the same shape as the ``actual_status`` incident (``LESSONS.md`` Lesson 8) — a
consumer reading a field no producer writes. This test is the cheap standing check that
the producer and the consumer still agree on the spelling.
"""

import re

import arf.scripts.utils.heartbeat as heartbeat_module
import arf.scripts.utils.prestep as prestep_module
from arf.scripts.verificators.common.paths import REPO_ROOT

STEP_TRACKER_SPEC_PATH = REPO_ROOT / "arf" / "specifications" / "step_tracker_specification.md"
SPEC_VERSION_HEADER_PATTERN = re.compile(r"^\*\*Version\*\*:\s*(\d+)\s*$", re.MULTILINE)


def test_step_tracker_field_names_agree_between_prestep_and_heartbeat() -> None:
    assert prestep_module.FIELD_STEPS == heartbeat_module.STEPS_FIELD
    assert prestep_module.FIELD_STEP == heartbeat_module.STEP_FIELD
    assert prestep_module.FIELD_STATUS == heartbeat_module.STATUS_FIELD
    assert prestep_module.FIELD_STARTED_AT == heartbeat_module.STARTED_AT_FIELD


def test_step_status_values_agree_between_prestep_and_heartbeat() -> None:
    assert prestep_module.STATUS_IN_PROGRESS == heartbeat_module.STATUS_IN_PROGRESS
    assert prestep_module.STATUS_COMPLETED == heartbeat_module.STATUS_COMPLETED


def test_stamped_spec_version_matches_the_specification_document() -> None:
    """``heartbeat`` stamps the version the specification actually declares.

    The constant's own comment says to keep it in step with
    ``step_tracker_specification.md``, and nothing enforced that: the stamp drifted to
    ``"5"`` while the document reached version 7 and its own examples showed ``"6"``.
    Three numbers for one thing, and every test still passed, because
    ``verify_step_liveness`` only checks that the field is *present* (the v1 carve-out)
    and never reads its value.

    That is the Lesson 8 shape again — an invariant addressed to whoever remembers it.
    This is the check that makes it hold.
    """
    spec_text: str = STEP_TRACKER_SPEC_PATH.read_text(encoding="utf-8")
    match = SPEC_VERSION_HEADER_PATTERN.search(spec_text)
    assert match is not None, f"{STEP_TRACKER_SPEC_PATH} declares a **Version**: header"

    declared_version: str = match.group(1)
    assert declared_version == heartbeat_module.STEP_TRACKER_SPEC_VERSION, (
        f"heartbeat stamps spec_version={heartbeat_module.STEP_TRACKER_SPEC_VERSION!r} "
        f"but the specification declares version {declared_version!r}. "
        "Bump STEP_TRACKER_SPEC_VERSION whenever the specification version changes."
    )


def test_specification_examples_use_the_stamped_spec_version() -> None:
    """The spec's own JSON examples show what ``heartbeat`` really writes.

    An example carrying a stale version is how a reader learns the wrong value, and how
    a fixture gets built against a schema the producer never writes.
    """
    spec_text: str = STEP_TRACKER_SPEC_PATH.read_text(encoding="utf-8")
    example_versions: set[str] = set(re.findall(r'"spec_version":\s*"(\d+)"', spec_text))
    unexpected: set[str] = example_versions - {heartbeat_module.STEP_TRACKER_SPEC_VERSION}
    assert len(unexpected) == 0, (
        f"{STEP_TRACKER_SPEC_PATH} shows spec_version {sorted(unexpected)} in an example, "
        f"but heartbeat stamps {heartbeat_module.STEP_TRACKER_SPEC_VERSION!r}."
    )
