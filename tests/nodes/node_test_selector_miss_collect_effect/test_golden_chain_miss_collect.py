# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the nightly miss collector, through the packaged contract."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml

from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.inmemory_adapters import (
    InMemoryAlarmReceiver,
    InMemoryAlarmStore,
    InMemoryNightlyGateway,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    EnumCollectStatus,
    EnumSlotStatus,
    ModelCollectRequest,
    ModelMissCollectSettings,
    ModelSlotCheckRequest,
    OwnerRoleUnsetError,
    load_policy,
)

pytestmark = pytest.mark.unit

NODE = "omnimarket.nodes.node_test_selector_miss_collect_effect"
SLOT = date(2026, 11, 21)
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
OVERLAY: dict[str, Any] = {
    "repo": "example-org/example-repo",
    "workflow_file": "nightly-full-suite.yml",
    "owner_role": "example-test-selection-owner",
    "alarm_route": "test_selector_miss",
    "collector_enabled": True,
    "status_tick": "example-lab-status-tick",
}


def _contract() -> dict[str, Any]:
    package = importlib.import_module(NODE)
    assert package.__file__ is not None
    return yaml.safe_load((Path(package.__file__).parent / "contract.yaml").read_text())


def _handler_class(entry: dict[str, Any]) -> Any:
    module = importlib.import_module(entry["handler"]["module"])
    return getattr(module, entry["handler"]["name"])


def test_contract_binds_each_operation_with_the_canonical_signature() -> None:
    for entry in _contract()["handler_routing"]["handlers"]:
        handler_class = _handler_class(entry)
        hints = get_type_hints(handler_class.handle)
        request_module, _, request_name = entry["input_model"].rpartition(".")
        result_module, _, result_name = entry["output_model"].rpartition(".")
        assert hints["request"] is getattr(
            importlib.import_module(request_module), request_name
        )
        assert hints["return"] is getattr(
            importlib.import_module(result_module), result_name
        )


def test_golden_a_night_with_no_gateway_run_records_nothing_and_the_slot_check_alarms() -> (
    None
):
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}
    settings = ModelMissCollectSettings.load(OVERLAY)
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    collect = _handler_class(entries["collect_nightly"])(
        settings=settings,
        policy=load_policy(),
        gateway=InMemoryNightlyGateway(run=None, reading=None, previous=None),
        store=store,
        receiver=receiver,
    )
    assert (
        collect.handle(ModelCollectRequest(slot_date=SLOT, now=NOW)).status
        is EnumCollectStatus.NO_RUN
    )
    slot = _handler_class(entries["check_slot_result"])(
        settings=settings, policy=load_policy(), store=store, receiver=receiver
    )
    result = slot.handle(ModelSlotCheckRequest(slot_date=SLOT, now=NOW))
    assert result.status is EnumSlotStatus.ALARMED
    assert len(receiver.received) == 1


def test_error_chain_an_overlay_without_an_owner_role_never_builds_a_handler() -> None:
    document = {k: v for k, v in OVERLAY.items() if k != "owner_role"}
    with pytest.raises(OwnerRoleUnsetError):
        ModelMissCollectSettings.load(document)


def test_error_chain_a_request_without_a_zone_aware_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ModelCollectRequest.model_validate(
            {"slot_date": SLOT, "now": "2026-11-22T12:00:00"}
        )
