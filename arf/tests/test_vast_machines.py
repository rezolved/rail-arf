"""Tests for arf.scripts.utils.vast_machines module.

Tests written before implementation to define the expected contract
for Vast.ai GPU provisioning utilities.
"""

import argparse
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest

from arf.scripts.utils.vast_machines import (
    CREATION_TIMEOUT_SECONDS,
    DEFAULT_FILTERS,
    GPU_SPEED_TIERS,
    MAX_RETRY_OFFERS,
    POLL_INTERVAL_SECONDS,
    RELIABILITY_THRESHOLDS,
    SSH_READY_PER_ATTEMPT_TIMEOUT_SECONDS,
    SSH_READY_POLL_INTERVAL_SECONDS,
    SSH_READY_TIMEOUT_SECONDS,
    DestroyResult,
    FailedAttempt,
    ProvisionResult,
    SearchCriteria,
    VastOffer,
    _build_onstart,
    build_query_string,
    destroy_and_confirm,
    label_instance,
    rank_offers,
    reliability_threshold_for,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_criteria(**overrides: Any) -> SearchCriteria:
    defaults: dict[str, Any] = {
        "gpu_name": None,
        "num_gpus": 1,
        "min_gpu_ram": None,
        "min_cpu_ram": None,
        "min_disk": None,
        "min_reliability": 0.95,
        "estimated_hours_reference": 2.0,
        "extra_filters": None,
    }
    defaults.update(overrides)
    return SearchCriteria(**defaults)


def _make_offer(**overrides: Any) -> VastOffer:
    defaults: dict[str, Any] = {
        "offer_id": 1,
        "gpu": "RTX 3090",
        "gpu_count": 1,
        "gpu_ram_gb": 24.0,
        "cpu_ram_gb": 64.0,
        "disk_gb": 100.0,
        "price_per_hour": 0.30,
        "reliability": 0.99,
        "location": "US",
        "compute_cap": 860,
        "cuda_max_good": 12.6,
    }
    defaults.update(overrides)
    return VastOffer(**defaults)


# ---------------------------------------------------------------------------
# 1. test_default_filters_constant
# ---------------------------------------------------------------------------


def test_default_filters_constant() -> None:
    assert "compute_cap<1200" in DEFAULT_FILTERS
    assert "cuda_max_good>=12.6" in DEFAULT_FILTERS


# ---------------------------------------------------------------------------
# 2. test_build_query_string_includes_default_filters
# ---------------------------------------------------------------------------


def test_build_query_string_includes_default_filters() -> None:
    criteria: SearchCriteria = _make_criteria()
    query: str = build_query_string(criteria=criteria)
    assert DEFAULT_FILTERS in query


# ---------------------------------------------------------------------------
# 3. test_build_query_string_includes_gpu_name
# ---------------------------------------------------------------------------


def test_build_query_string_includes_gpu_name() -> None:
    criteria: SearchCriteria = _make_criteria(gpu_name="RTX_4090")
    query: str = build_query_string(criteria=criteria)
    assert "gpu_name=RTX_4090" in query


# ---------------------------------------------------------------------------
# 4. test_build_query_string_omits_null_gpu_name
# ---------------------------------------------------------------------------


def test_build_query_string_omits_null_gpu_name() -> None:
    criteria: SearchCriteria = _make_criteria(gpu_name=None)
    query: str = build_query_string(criteria=criteria)
    assert "gpu_name" not in query


# ---------------------------------------------------------------------------
# 5. test_rank_offers_fastest_first
# ---------------------------------------------------------------------------


def test_rank_offers_fastest_first() -> None:
    slow: VastOffer = _make_offer(
        offer_id=1,
        gpu="RTX 3090",
        price_per_hour=0.30,
    )
    medium: VastOffer = _make_offer(
        offer_id=2,
        gpu="RTX 4090",
        price_per_hour=0.50,
    )
    fast: VastOffer = _make_offer(
        offer_id=3,
        gpu="H100",
        price_per_hour=2.00,
    )
    ranked: list[VastOffer] = rank_offers(
        offers=[slow, fast, medium],
        estimated_hours_reference=10.0,
    )
    gpu_order: list[str] = [o.gpu for o in ranked]
    assert gpu_order.index("H100") < gpu_order.index("RTX 4090")
    assert gpu_order.index("RTX 4090") < gpu_order.index("RTX 3090")


# ---------------------------------------------------------------------------
# 6. test_rank_offers_prefers_cheaper_when_similar_speed
# ---------------------------------------------------------------------------


def test_rank_offers_prefers_cheaper_when_similar_speed() -> None:
    cheap: VastOffer = _make_offer(
        offer_id=1,
        gpu="RTX 3090",
        price_per_hour=0.20,
    )
    expensive: VastOffer = _make_offer(
        offer_id=2,
        gpu="RTX 3090",
        price_per_hour=0.50,
    )
    ranked: list[VastOffer] = rank_offers(
        offers=[expensive, cheap],
        estimated_hours_reference=2.0,
    )
    assert ranked[0].offer_id == cheap.offer_id


# ---------------------------------------------------------------------------
# 7. test_reliability_threshold_short_job
# ---------------------------------------------------------------------------


def test_reliability_threshold_short_job() -> None:
    threshold: float = reliability_threshold_for(estimated_hours=0.5)
    assert threshold == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# 8. test_reliability_threshold_medium_job
# ---------------------------------------------------------------------------


def test_reliability_threshold_medium_job() -> None:
    threshold: float = reliability_threshold_for(estimated_hours=3.0)
    assert threshold == pytest.approx(0.98)


# ---------------------------------------------------------------------------
# 9. test_reliability_threshold_long_job
# ---------------------------------------------------------------------------


def test_reliability_threshold_long_job() -> None:
    threshold: float = reliability_threshold_for(estimated_hours=10.0)
    assert threshold == pytest.approx(0.995)


# ---------------------------------------------------------------------------
# 10. test_reliability_threshold_very_long_job
# ---------------------------------------------------------------------------


def test_reliability_threshold_very_long_job() -> None:
    threshold: float = reliability_threshold_for(estimated_hours=30.0)
    assert threshold == pytest.approx(0.999)


# ---------------------------------------------------------------------------
# 11. test_gpu_speed_tiers_has_reference_gpu
# ---------------------------------------------------------------------------


def test_gpu_speed_tiers_has_reference_gpu() -> None:
    assert "RTX 3090" in GPU_SPEED_TIERS
    assert GPU_SPEED_TIERS["RTX 3090"] == 1.0


# ---------------------------------------------------------------------------
# 12. test_failed_attempt_dataclass_frozen
# ---------------------------------------------------------------------------


def test_failed_attempt_dataclass_frozen() -> None:
    attempt: FailedAttempt = FailedAttempt(
        offer_id=1,
        instance_id=None,
        gpu="RTX 3090",
        failure_reason="timeout",
        failure_phase="creation",
        duration_seconds=120.0,
        wasted_cost_usd=0.02,
        timestamp="2026-04-12T00:00:00Z",
    )
    with pytest.raises(FrozenInstanceError):
        attempt.offer_id = 2  # type: ignore[misc]  # noqa: B003


# ---------------------------------------------------------------------------
# 13. test_provision_result_has_timing_fields
# ---------------------------------------------------------------------------


def test_provision_result_has_timing_fields() -> None:
    result: ProvisionResult = ProvisionResult(
        instance_id="123",
        offer=_make_offer(),
        ssh_host="1.2.3.4",
        ssh_port=22,
        gpu_verified="RTX 3090",
        cuda_version="12.6",
        label="test-label",
        created_at="2026-04-12T00:00:00Z",
        ready_at="2026-04-12T00:05:00Z",
        search_started_at="2026-04-12T00:00:00Z",
        total_provisioning_seconds=300.0,
        failed_attempts=[],
    )
    assert result.search_started_at == "2026-04-12T00:00:00Z"
    assert result.total_provisioning_seconds == 300.0


# ---------------------------------------------------------------------------
# 14. test_label_instance_calls_sdk
# ---------------------------------------------------------------------------


def test_label_instance_calls_sdk() -> None:
    mock_sdk: MagicMock = MagicMock()
    label_instance(
        sdk=mock_sdk,
        instance_id="12345",
        label="my-task",
    )
    mock_sdk.label_instance.assert_called_once_with(
        id="12345",
        label="my-task",
    )


# ---------------------------------------------------------------------------
# 15. test_destroy_and_confirm_returns_cost
# ---------------------------------------------------------------------------


def test_destroy_and_confirm_returns_cost() -> None:
    mock_sdk: MagicMock = MagicMock()
    mock_sdk.destroy_instance.return_value = {"success": True}
    mock_sdk.show_instances.return_value = []

    result: DestroyResult = destroy_and_confirm(
        sdk=mock_sdk,
        instance_id="12345",
        created_at="2026-04-12T00:00:00Z",
        price_per_hour=0.50,
    )
    assert result.instance_id == "12345"
    assert isinstance(result.destroyed_at, str)
    assert len(result.destroyed_at) > 0
    assert isinstance(result.total_duration_hours, float)
    assert isinstance(result.total_cost_usd, float)
    mock_sdk.destroy_instance.assert_called_once()


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


def test_constants_values() -> None:
    assert MAX_RETRY_OFFERS == 3
    assert pytest.approx(30.0) == POLL_INTERVAL_SECONDS
    assert pytest.approx(600.0) == CREATION_TIMEOUT_SECONDS
    assert len(RELIABILITY_THRESHOLDS) == 4


# ---------------------------------------------------------------------------
# init_sdk
# ---------------------------------------------------------------------------


def test_init_sdk_reads_env_when_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    monkeypatch.setenv("VAST_API_KEY", "env-key-12345")
    captured: dict[str, Any] = {}

    def fake_factory(*, api_key: str) -> MagicMock:
        captured["api_key"] = api_key
        return MagicMock()

    monkeypatch.setattr(vast_machines, "_sdk_factory", fake_factory, raising=False)
    handle: Any = vast_machines.init_sdk()
    assert handle is not None
    assert captured["api_key"] == "env-key-12345"


def test_init_sdk_uses_explicit_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    monkeypatch.setenv("VAST_API_KEY", "env-key-should-be-ignored")
    captured: dict[str, Any] = {}

    def fake_factory(*, api_key: str) -> MagicMock:
        captured["api_key"] = api_key
        return MagicMock()

    monkeypatch.setattr(vast_machines, "_sdk_factory", fake_factory, raising=False)
    vast_machines.init_sdk(api_key="explicit-key")
    assert captured["api_key"] == "explicit-key"


def test_init_sdk_raises_when_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    monkeypatch.delenv("VAST_API_KEY", raising=False)
    with pytest.raises((RuntimeError, ValueError, KeyError)):
        vast_machines.init_sdk()


# ---------------------------------------------------------------------------
# search_offers
# ---------------------------------------------------------------------------


def _canned_offer_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1001,
        "gpu_name": "RTX 3090",
        "num_gpus": 1,
        "gpu_ram": 24576,
        "cpu_ram": 65536,
        "disk_space": 200.0,
        "dph_total": 0.30,
        "reliability2": 0.99,
        "geolocation": "US",
        "compute_cap": 860,
        "cuda_max_good": 12.8,
    }
    row.update(overrides)
    return row


def test_search_offers_injects_default_filters() -> None:
    from arf.scripts.utils import vast_machines

    mock_sdk: MagicMock = MagicMock()
    mock_sdk.search_offers.return_value = [_canned_offer_row()]
    criteria: SearchCriteria = _make_criteria()
    vast_machines.search_offers(sdk=mock_sdk, criteria=criteria)
    assert mock_sdk.search_offers.called
    call_kwargs: dict[str, Any] = mock_sdk.search_offers.call_args.kwargs
    query: str = call_kwargs.get("query", "")
    assert DEFAULT_FILTERS in query


def test_search_offers_returns_ranked_list() -> None:
    from arf.scripts.utils import vast_machines

    rows: list[dict[str, Any]] = [
        _canned_offer_row(id=1, gpu_name="RTX 3090", dph_total=0.30),
        _canned_offer_row(id=2, gpu_name="H100", dph_total=2.00),
    ]
    mock_sdk: MagicMock = MagicMock()
    mock_sdk.search_offers.return_value = rows
    offers: list[VastOffer] = vast_machines.search_offers(
        sdk=mock_sdk,
        criteria=_make_criteria(estimated_hours_reference=10.0),
    )
    assert len(offers) == 2
    # H100 ranks first (fastest, lowest total cost on long jobs).
    assert offers[0].gpu == "H100"


def test_search_offers_makes_no_network_call_without_sdk() -> None:
    """search_offers must delegate entirely to the sdk object passed in."""
    from arf.scripts.utils import vast_machines

    mock_sdk: MagicMock = MagicMock()
    mock_sdk.search_offers.return_value = []
    result: list[VastOffer] = vast_machines.search_offers(
        sdk=mock_sdk,
        criteria=_make_criteria(),
    )
    assert result == []
    assert mock_sdk.search_offers.call_count == 1


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------


def _stub_sdk_for_acquire(
    *,
    offer_rows: list[dict[str, Any]],
    create_outcomes: list[Any],
    status_after_create: str = "running",
) -> MagicMock:
    """Build a mock SDK that returns canned offers and create-instance outcomes."""
    sdk: MagicMock = MagicMock()
    sdk.search_offers.return_value = offer_rows
    sdk.create_instance.side_effect = create_outcomes
    sdk.show_instance.return_value = {
        "actual_status": status_after_create,
        "ssh_host": "ssh.vast.ai",
        "ssh_port": 12345,
    }
    sdk.destroy_instance.return_value = {"success": True}
    sdk.label_instance.return_value = {"success": True}
    return sdk


def test_acquire_happy_path_first_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    offer_rows: list[dict[str, Any]] = [_canned_offer_row(id=5001)]
    sdk: MagicMock = _stub_sdk_for_acquire(
        offer_rows=offer_rows,
        create_outcomes=[{"new_contract": "i-9000", "success": True}],
    )

    # Stub the GPU verification step so the test does not SSH.
    def fake_verify(*, sdk: Any, instance_id: str) -> dict[str, str]:
        return {"gpu_verified": "RTX 3090", "cuda_version": "12.8"}

    monkeypatch.setattr(vast_machines, "_verify_gpu_via_ssh", fake_verify, raising=False)

    result: ProvisionResult = vast_machines.acquire(
        sdk=sdk,
        task_id="t0099_demo",
        criteria=_make_criteria(),
        image="pytorch/pytorch:2.6.0-cuda12.6-cudnn9-devel",
        disk_gb=100,
        label="proj/t0099_demo",
    )
    assert result.instance_id == "i-9000"
    assert result.failed_attempts == []
    assert result.gpu_verified == "RTX 3090"


def test_acquire_one_failed_attempt_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    offer_rows: list[dict[str, Any]] = [
        _canned_offer_row(id=5001, gpu_name="RTX 5090"),
        _canned_offer_row(id=5002, gpu_name="RTX 3090"),
    ]
    sdk: MagicMock = _stub_sdk_for_acquire(
        offer_rows=offer_rows,
        create_outcomes=[
            {"new_contract": "i-bad", "success": True},
            {"new_contract": "i-good", "success": True},
        ],
    )

    verify_calls: list[str] = []

    def fake_verify(*, sdk: Any, instance_id: str) -> dict[str, str]:
        verify_calls.append(instance_id)
        if instance_id == "i-bad":
            raise RuntimeError("sm_120 compute capability not supported")
        return {"gpu_verified": "RTX 3090", "cuda_version": "12.8"}

    monkeypatch.setattr(vast_machines, "_verify_gpu_via_ssh", fake_verify, raising=False)

    result: ProvisionResult = vast_machines.acquire(
        sdk=sdk,
        task_id="t0099_demo",
        criteria=_make_criteria(),
        image="pytorch/pytorch:2.6.0-cuda12.6-cudnn9-devel",
        disk_gb=100,
        label="proj/t0099_demo",
    )
    assert result.instance_id == "i-good"
    assert len(result.failed_attempts) == 1
    failure: FailedAttempt = result.failed_attempts[0]
    assert failure.offer_id == 5001
    assert failure.failure_phase in {"gpu_verification", "creation", "ssh", "waiting"}


def test_acquire_all_offers_fail_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    offer_rows: list[dict[str, Any]] = [
        _canned_offer_row(id=5001),
        _canned_offer_row(id=5002),
        _canned_offer_row(id=5003),
    ]
    sdk: MagicMock = _stub_sdk_for_acquire(
        offer_rows=offer_rows,
        create_outcomes=[
            {"new_contract": "i-1", "success": True},
            {"new_contract": "i-2", "success": True},
            {"new_contract": "i-3", "success": True},
        ],
    )

    def always_fail(*, sdk: Any, instance_id: str) -> dict[str, str]:
        raise RuntimeError("permanent gpu failure")

    monkeypatch.setattr(vast_machines, "_verify_gpu_via_ssh", always_fail, raising=False)

    error_cls: type[Exception] = getattr(vast_machines, "VastProvisioningError", RuntimeError)
    with pytest.raises(error_cls):
        vast_machines.acquire(
            sdk=sdk,
            task_id="t0099_demo",
            criteria=_make_criteria(),
            image="pytorch/pytorch:2.6.0-cuda12.6-cudnn9-devel",
            disk_gb=100,
            label="proj/t0099_demo",
        )


def test_acquire_stops_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acquire must not try more than MAX_RETRY_OFFERS offers."""
    from arf.scripts.utils import vast_machines

    offer_rows: list[dict[str, Any]] = [_canned_offer_row(id=6000 + i) for i in range(10)]
    sdk: MagicMock = _stub_sdk_for_acquire(
        offer_rows=offer_rows,
        create_outcomes=[{"new_contract": f"i-{i}", "success": True} for i in range(10)],
    )
    attempts: list[str] = []

    def always_fail(*, sdk: Any, instance_id: str) -> dict[str, str]:
        attempts.append(instance_id)
        raise RuntimeError("permanent gpu failure")

    monkeypatch.setattr(vast_machines, "_verify_gpu_via_ssh", always_fail, raising=False)
    error_cls: type[Exception] = getattr(vast_machines, "VastProvisioningError", RuntimeError)
    with pytest.raises(error_cls):
        vast_machines.acquire(
            sdk=sdk,
            task_id="t0099_demo",
            criteria=_make_criteria(),
            image="img",
            disk_gb=100,
            label="proj/t0099_demo",
        )
    assert len(attempts) <= MAX_RETRY_OFFERS


# ---------------------------------------------------------------------------
# to_machine_log_entry
# ---------------------------------------------------------------------------


def _make_provision_result(**overrides: Any) -> ProvisionResult:
    defaults: dict[str, Any] = {
        "instance_id": "i-9000",
        "offer": _make_offer(offer_id=5001),
        "ssh_host": "ssh.vast.ai",
        "ssh_port": 12345,
        "gpu_verified": "RTX 3090",
        "cuda_version": "12.8",
        "label": "proj/t0099_demo",
        "created_at": "2026-04-12T00:00:00Z",
        "ready_at": "2026-04-12T00:05:00Z",
        "search_started_at": "2026-04-12T00:00:00Z",
        "total_provisioning_seconds": 300.0,
        "failed_attempts": [],
    }
    defaults.update(overrides)
    return ProvisionResult(**defaults)


def test_to_machine_log_entry_without_destroy_result() -> None:
    from arf.scripts.utils import vast_machines

    entry: dict[str, Any] = vast_machines.to_machine_log_entry(
        provision_result=_make_provision_result(),
    )
    assert entry["provider"] == "vast_ai"
    assert entry["instance_id"] == "i-9000"
    assert entry["offer_id"] == 5001
    assert entry["destroyed_at"] is None
    assert entry["total_duration_hours"] is None
    assert entry["total_cost_usd"] is None
    assert entry["selected_offer"]["offer_id"] == 5001
    assert entry["search_started_at"] == "2026-04-12T00:00:00Z"
    assert entry["total_provisioning_seconds"] == 300.0
    assert entry["failed_attempts"] == []


def test_to_machine_log_entry_with_destroy_result() -> None:
    from arf.scripts.utils import vast_machines

    destroy: DestroyResult = DestroyResult(
        instance_id="i-9000",
        destroyed_at="2026-04-12T01:05:00Z",
        total_duration_hours=1.0,
        total_cost_usd=0.30,
    )
    entry: dict[str, Any] = vast_machines.to_machine_log_entry(
        provision_result=_make_provision_result(),
        destroy_result=destroy,
    )
    assert entry["destroyed_at"] == "2026-04-12T01:05:00Z"
    assert entry["total_duration_hours"] == pytest.approx(1.0)
    assert entry["total_cost_usd"] == pytest.approx(0.30)


def test_to_machine_log_entry_includes_failed_attempts() -> None:
    from arf.scripts.utils import vast_machines

    failure: FailedAttempt = FailedAttempt(
        offer_id=5001,
        instance_id="i-bad",
        gpu="RTX 5090",
        failure_reason="sm_120 not supported",
        failure_phase="gpu_verification",
        duration_seconds=180.0,
        wasted_cost_usd=0.12,
        timestamp="2026-04-12T00:00:00Z",
    )
    entry: dict[str, Any] = vast_machines.to_machine_log_entry(
        provision_result=_make_provision_result(failed_attempts=[failure]),
    )
    assert len(entry["failed_attempts"]) == 1
    rec: dict[str, Any] = entry["failed_attempts"][0]
    for field in (
        "offer_id",
        "instance_id",
        "gpu",
        "failure_reason",
        "failure_phase",
        "duration_seconds",
        "wasted_cost_usd",
        "timestamp",
    ):
        assert field in rec


def test_to_machine_log_entry_schema_field_presence() -> None:
    """Every field required by remote_machines_specification.md must appear."""
    from arf.scripts.utils import vast_machines

    entry: dict[str, Any] = vast_machines.to_machine_log_entry(
        provision_result=_make_provision_result(),
    )
    required_fields: list[str] = [
        "provider",
        "instance_id",
        "offer_id",
        "search_criteria",
        "selected_offer",
        "selection_rationale",
        "image",
        "disk_gb",
        "label",
        "ssh_host",
        "ssh_port",
        "gpu_verified",
        "cuda_version",
        "created_at",
        "ready_at",
        "destroyed_at",
        "total_duration_hours",
        "total_cost_usd",
        "search_started_at",
        "total_provisioning_seconds",
        "failed_attempts",
    ]
    for field in required_fields:
        assert field in entry, f"missing field: {field}"


def test_to_machine_log_entry_provider_string() -> None:
    """provider must be the canonical vast_ai slug."""
    from arf.scripts.utils import vast_machines

    entry: dict[str, Any] = vast_machines.to_machine_log_entry(
        provision_result=_make_provision_result(),
    )
    assert entry["provider"] == "vast_ai"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_cli_main_callable_exists() -> None:
    from arf.scripts.utils import vast_machines

    assert callable(getattr(vast_machines, "main", None))


def test_cli_acquire_exit_code_ok(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from arf.scripts.utils import vast_machines

    def fake_acquire(**kwargs: Any) -> ProvisionResult:
        return _make_provision_result()

    def fake_init_sdk(**kwargs: Any) -> MagicMock:
        return MagicMock()

    monkeypatch.setattr(vast_machines, "acquire", fake_acquire, raising=False)
    monkeypatch.setattr(vast_machines, "init_sdk", fake_init_sdk, raising=False)
    exit_code: int = vast_machines.main(["acquire", "t0099_demo"])
    assert exit_code == 0
    out: str = capsys.readouterr().out
    payload: dict[str, Any] = json.loads(out)
    assert payload.get("instance_id") == "i-9000"


def test_cli_acquire_exit_code_pool_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    error_cls: type[Exception] = getattr(vast_machines, "VastProvisioningError", RuntimeError)

    def busy_acquire(**kwargs: Any) -> ProvisionResult:
        raise error_cls("no offers within budget after retries")

    def fake_init_sdk(**kwargs: Any) -> MagicMock:
        return MagicMock()

    monkeypatch.setattr(vast_machines, "acquire", busy_acquire, raising=False)
    monkeypatch.setattr(vast_machines, "init_sdk", fake_init_sdk, raising=False)
    exit_code: int = vast_machines.main(["acquire", "t0099_demo"])
    assert exit_code == 75


def test_cli_teardown_exit_code_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    def fake_destroy(**kwargs: Any) -> DestroyResult:
        return DestroyResult(
            instance_id="i-9000",
            destroyed_at="2026-04-12T01:05:00Z",
            total_duration_hours=1.0,
            total_cost_usd=0.30,
        )

    def fake_init_sdk(**kwargs: Any) -> MagicMock:
        return MagicMock()

    monkeypatch.setattr(vast_machines, "destroy_and_confirm", fake_destroy, raising=False)
    monkeypatch.setattr(vast_machines, "init_sdk", fake_init_sdk, raising=False)
    exit_code: int = vast_machines.main(
        [
            "teardown",
            "t0099_demo",
            "--acquired-at",
            "2026-04-12T00:00:00Z",
        ]
    )
    assert exit_code == 0


def test_cli_generic_error_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arf.scripts.utils import vast_machines

    def boom(**kwargs: Any) -> ProvisionResult:
        raise RuntimeError("unexpected failure")

    def fake_init_sdk(**kwargs: Any) -> MagicMock:
        return MagicMock()

    monkeypatch.setattr(vast_machines, "acquire", boom, raising=False)
    monkeypatch.setattr(vast_machines, "init_sdk", fake_init_sdk, raising=False)
    exit_code: int = vast_machines.main(["acquire", "t0099_demo"])
    assert exit_code == 1


def test_cli_module_invokable_as_subprocess() -> None:
    """The module must be runnable via `python -m arf.scripts.utils.vast_machines`."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-m", "arf.scripts.utils.vast_machines", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    combined: str = completed.stdout + completed.stderr
    assert "acquire" in combined
    assert "teardown" in combined


# ---------------------------------------------------------------------------
# SSH-ready polling handshake (_verify_gpu_via_ssh)
# ---------------------------------------------------------------------------


def _make_ssh_sdk(*, ssh_host: str = "1.2.3.4", ssh_port: int = 22) -> MagicMock:
    sdk: MagicMock = MagicMock()
    sdk.show_instance.return_value = {
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
    }
    return sdk


def _success_completed_process() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess[str](
        args=["ssh"],
        returncode=0,
        stdout="NVIDIA H200, 535.183.06\n",
        stderr="",
    )


def test_verify_gpu_via_ssh_succeeds_on_first_attempt() -> None:
    from arf.scripts.utils import vast_machines

    sdk: MagicMock = _make_ssh_sdk()
    runner_calls: list[list[str]] = []
    sleep_calls: list[float] = []

    def ssh_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        runner_calls.append(cmd)
        return _success_completed_process()

    def sleep_func(seconds: float) -> None:
        sleep_calls.append(seconds)

    monotonic_values: list[float] = [0.0, 1.0]
    monotonic_index: list[int] = [0]

    def monotonic_func() -> float:
        idx: int = monotonic_index[0]
        monotonic_index[0] = min(idx + 1, len(monotonic_values) - 1)
        return monotonic_values[idx]

    result: dict[str, str] = vast_machines._verify_gpu_via_ssh(
        sdk=sdk,
        instance_id="i-1",
        ssh_runner=ssh_runner,
        sleep_func=sleep_func,
        monotonic_func=monotonic_func,
    )
    assert result["gpu_verified"] == "NVIDIA H200"
    assert result["cuda_version"] == "535.183.06"
    assert len(runner_calls) == 1
    assert len(sleep_calls) == 0


def test_verify_gpu_via_ssh_succeeds_after_n_failures() -> None:
    from arf.scripts.utils import vast_machines

    sdk: MagicMock = _make_ssh_sdk()
    runner_calls: list[list[str]] = []
    sleep_calls: list[float] = []
    attempt_counter: list[int] = [0]

    def ssh_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        runner_calls.append(cmd)
        attempt_counter[0] += 1
        if attempt_counter[0] <= 3:
            raise subprocess.CalledProcessError(
                returncode=255,
                cmd=cmd,
                output="",
                stderr="ssh: Connection refused",
            )
        return _success_completed_process()

    def sleep_func(seconds: float) -> None:
        sleep_calls.append(seconds)

    monotonic_values: list[float] = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    monotonic_index: list[int] = [0]

    def monotonic_func() -> float:
        idx: int = monotonic_index[0]
        monotonic_index[0] = min(idx + 1, len(monotonic_values) - 1)
        return monotonic_values[idx]

    result: dict[str, str] = vast_machines._verify_gpu_via_ssh(
        sdk=sdk,
        instance_id="i-1",
        ssh_runner=ssh_runner,
        sleep_func=sleep_func,
        monotonic_func=monotonic_func,
    )
    assert result["gpu_verified"] == "NVIDIA H200"
    assert len(runner_calls) == 4
    assert len(sleep_calls) == 3
    for s in sleep_calls:
        assert s == pytest.approx(float(SSH_READY_POLL_INTERVAL_SECONDS))


def test_verify_gpu_via_ssh_exhausts_budget() -> None:
    from arf.scripts.utils import vast_machines

    sdk: MagicMock = _make_ssh_sdk()
    attempt_counter: list[int] = [0]

    def ssh_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        attempt_counter[0] += 1
        raise subprocess.CalledProcessError(
            returncode=255,
            cmd=cmd,
            output="",
            stderr="ssh: Permission denied (publickey)",
        )

    sleep_calls: list[float] = []

    def sleep_func(seconds: float) -> None:
        sleep_calls.append(seconds)

    # monotonic returns 0 for the start, then values that keep deadline alive
    # for several iterations, then jumps past the deadline. The loop calls
    # monotonic twice per iteration (while-condition and post-attempt check).
    monotonic_sequence: list[float] = [
        0.0,  # start (deadline = SSH_READY_TIMEOUT_SECONDS)
        10.0,  # iter 1 while-check
        20.0,  # iter 1 post-attempt check
        30.0,  # iter 2 while-check
        40.0,  # iter 2 post-attempt check
        50.0,  # iter 3 while-check
        60.0,  # iter 3 post-attempt check
        70.0,  # iter 4 while-check
        float(SSH_READY_TIMEOUT_SECONDS) + 1.0,  # iter 4 post-attempt: exhausted
        float(SSH_READY_TIMEOUT_SECONDS) + 2.0,  # final elapsed calc
    ]
    monotonic_index: list[int] = [0]

    def monotonic_func() -> float:
        idx: int = monotonic_index[0]
        monotonic_index[0] = min(idx + 1, len(monotonic_sequence) - 1)
        return monotonic_sequence[idx]

    with pytest.raises(RuntimeError) as excinfo:
        vast_machines._verify_gpu_via_ssh(
            sdk=sdk,
            instance_id="i-1",
            ssh_runner=ssh_runner,
            sleep_func=sleep_func,
            monotonic_func=monotonic_func,
        )

    message: str = str(excinfo.value)
    assert "attempt" in message.lower()
    assert str(attempt_counter[0]) in message
    assert "Permission denied" in message
    assert attempt_counter[0] >= 3


def test_verify_gpu_via_ssh_handles_subprocess_timeout() -> None:
    from arf.scripts.utils import vast_machines

    sdk: MagicMock = _make_ssh_sdk()
    attempt_counter: list[int] = [0]

    def ssh_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        attempt_counter[0] += 1
        if attempt_counter[0] == 1:
            raise subprocess.TimeoutExpired(
                cmd=cmd,
                timeout=float(SSH_READY_PER_ATTEMPT_TIMEOUT_SECONDS),
            )
        return _success_completed_process()

    sleep_calls: list[float] = []

    def sleep_func(seconds: float) -> None:
        sleep_calls.append(seconds)

    monotonic_values: list[float] = [0.0, 1.0, 2.0, 3.0]
    monotonic_index: list[int] = [0]

    def monotonic_func() -> float:
        idx: int = monotonic_index[0]
        monotonic_index[0] = min(idx + 1, len(monotonic_values) - 1)
        return monotonic_values[idx]

    result: dict[str, str] = vast_machines._verify_gpu_via_ssh(
        sdk=sdk,
        instance_id="i-1",
        ssh_runner=ssh_runner,
        sleep_func=sleep_func,
        monotonic_func=monotonic_func,
    )
    assert result["gpu_verified"] == "NVIDIA H200"
    assert attempt_counter[0] == 2
    assert len(sleep_calls) == 1


def test_ssh_ready_constants_are_sane() -> None:
    assert SSH_READY_POLL_INTERVAL_SECONDS == 15
    assert SSH_READY_TIMEOUT_SECONDS == 300
    assert SSH_READY_PER_ATTEMPT_TIMEOUT_SECONDS == 30
    assert SSH_READY_TIMEOUT_SECONDS >= CREATION_TIMEOUT_SECONDS / 2


# ---------------------------------------------------------------------------
# Idle-watchdog onstart wiring
# ---------------------------------------------------------------------------


def test_build_onstart_returns_none_when_watchdog_disabled() -> None:
    args: argparse.Namespace = argparse.Namespace(
        enable_idle_watchdog=False,
        idle_threshold_seconds=300,
        grace_seconds=30,
        poll_interval_seconds=20,
    )
    assert _build_onstart(args=args) is None


def test_build_onstart_embeds_threshold_and_container_key_self_destroy() -> None:
    args: argparse.Namespace = argparse.Namespace(
        enable_idle_watchdog=True,
        idle_threshold_seconds=300,
        grace_seconds=30,
        poll_interval_seconds=20,
    )
    onstart: str | None = _build_onstart(args=args)
    assert onstart is not None
    assert "IDLE_THRESHOLD_SECONDS=300" in onstart
    # Self-destroy uses Vast's per-instance CONTAINER_API_KEY, no manual key.
    assert 'vastai destroy instance "$CONTAINER_ID"' in onstart
    assert "$CONTAINER_API_KEY" in onstart
    assert "ARF_WATCHDOG_KEY" not in onstart
