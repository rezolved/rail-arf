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

import arf.scripts.utils.heartbeat as heartbeat_module
import arf.scripts.utils.prestep as prestep_module


def test_step_tracker_field_names_agree_between_prestep_and_heartbeat() -> None:
    assert prestep_module.FIELD_STEPS == heartbeat_module.STEPS_FIELD
    assert prestep_module.FIELD_STEP == heartbeat_module.STEP_FIELD
    assert prestep_module.FIELD_STATUS == heartbeat_module.STATUS_FIELD
    assert prestep_module.FIELD_STARTED_AT == heartbeat_module.STARTED_AT_FIELD


def test_step_status_values_agree_between_prestep_and_heartbeat() -> None:
    assert prestep_module.STATUS_IN_PROGRESS == heartbeat_module.STATUS_IN_PROGRESS
    assert prestep_module.STATUS_COMPLETED == heartbeat_module.STATUS_COMPLETED
