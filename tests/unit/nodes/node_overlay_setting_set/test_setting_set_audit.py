# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A set reads the store first, writes one record the projection stores, and leaves one audit row naming who set it."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.events.overlay_setting_audit import (
    ModelOverlaySettingAudit,
)
from omnimarket.models.overlay_store import ModelOverlayStoreRecord
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    ACTOR,
    BOOT_SET,
    KEY,
    NOW,
    OLD_LAZY_START,
    OVERLAY_ID,
    PATH,
    build,
    contract,
    live_record,
)

pytestmark = pytest.mark.unit


def request(
    value: object = BOOT_SET, actor: str = ACTOR
) -> ModelOverlaySettingSetRequest:
    return ModelOverlaySettingSetRequest.model_validate(
        {"overlay_id": OVERLAY_ID, "key": KEY, "value": value, "actor": actor}
    )


async def test_audit_row_names_the_actor_the_previous_value_and_the_new_value() -> None:
    handler, bus, _ = build()
    await handler.handle(request())
    topics = contract()["event_bus"]["publish_topics"]
    rows = bus.on(topics[1])
    assert len(rows) == 1
    key, value = rows[0]
    assert key == f"{OVERLAY_ID}:{KEY}".encode()
    audit = ModelOverlaySettingAudit.model_validate_json(value)
    assert audit.actor == ACTOR
    assert audit.previous_value == OLD_LAZY_START
    assert audit.previous_present is True
    assert audit.new_value == BOOT_SET
    assert audit.overlay_id == OVERLAY_ID
    assert audit.key == KEY
    assert audit.setting_path == PATH
    assert audit.set_at == NOW


async def test_the_audit_row_says_when_there_was_no_previous_value() -> None:
    document = live_record()["document"]
    del document["lanes"]["lane-a"]["lazy_start"]
    handler, bus, _ = build(live={OVERLAY_ID: live_record(document)})
    await handler.handle(request())
    audit = ModelOverlaySettingAudit.model_validate_json(
        bus.on(contract()["event_bus"]["publish_topics"][1])[0][1]
    )
    assert audit.previous_present is False
    assert audit.previous_value is None


async def test_the_store_is_read_before_anything_is_written() -> None:
    handler, _, reader = build()
    await handler.handle(request())
    assert reader.log[0] == f"read:{OVERLAY_ID}"
    assert reader.log[1:] == [
        f"publish:{t}" for t in contract()["event_bus"]["publish_topics"]
    ]


async def test_the_record_is_keyed_by_overlay_id_carries_the_new_value_and_marks_the_path() -> (
    None
):
    handler, bus, _ = build()
    result = await handler.handle(request())
    loaded_topic = contract()["event_bus"]["publish_topics"][0]
    ((key, value),) = bus.on(loaded_topic)
    assert key == OVERLAY_ID.encode()
    payload = json.loads(value)
    assert payload["operator_paths"] == [PATH]
    # The record is a whole record of the store: its digest is its document's.
    base = ModelOverlayStoreRecord.model_validate(
        {k: v for k, v in payload.items() if k != "operator_paths"}
    )
    assert base.loaded_by == ACTOR
    assert base.loaded_at == NOW
    assert base.source_ref == "runtime_contract_hotload@abc123"
    lanes: Any = base.document["lanes"]
    held: Any = live_record()["document"]
    assert lanes["lane-a"]["lazy_start"] == BOOT_SET
    # Every other setting is as the live record held it.
    assert lanes["lane-b"] == held["lanes"]["lane-b"]
    assert lanes["lane-a"]["mode"] == "on_demand"
    assert result.actor == ACTOR
    assert result.previous_value == OLD_LAZY_START
    assert result.new_value == BOOT_SET
    assert result.digest == base.digest


async def test_paths_an_operator_set_before_stay_marked() -> None:
    earlier = "lanes/lane-b/mode"
    handler, bus, _ = build(live={OVERLAY_ID: live_record(operator_paths=(earlier,))})
    await handler.handle(request())
    payload = json.loads(bus.on(contract()["event_bus"]["publish_topics"][0])[0][1])
    assert payload["operator_paths"] == sorted([earlier, PATH])


async def test_the_previous_value_restores_through_the_same_command() -> None:
    handler, _, _ = build()
    first = await handler.handle(request())
    # Restore: the same command, the value the first call read from the store before it wrote.
    handler_two, bus_two, _ = build()
    await handler_two.handle(request(first.previous_value, actor="restore"))
    payload = json.loads(bus_two.on(contract()["event_bus"]["publish_topics"][0])[0][1])
    assert payload["document"]["lanes"]["lane-a"]["lazy_start"] == OLD_LAZY_START
    assert payload["loaded_by"] == "restore"


def test_the_contract_publishes_the_record_topic_and_the_audit_topic() -> None:
    topics = contract()["event_bus"]["publish_topics"]
    assert topics == [
        "onex.evt.omnimarket.overlay-store-loaded.v1",
        "onex.evt.omnimarket.overlay-setting-set.v1",
    ]
