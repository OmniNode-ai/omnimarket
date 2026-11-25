# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The goal scope ticket rule's ``ticket_roots`` is a declared setting: a valid list is set through the store, an invalid one is refused with nothing written."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import JsonValue

from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    SettingValueInvalidError,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    ACTOR,
    LIVE_LOADED_AT,
    build,
    contract,
)

pytestmark = pytest.mark.unit

OVERLAY_ID = "goal_scope_ticket_rule"
KEY = "ticket_roots"
MODEL = "omnimarket.models.overlay_documents.model_goal_scope_ticket_rule_overlay:ModelGoalScopeTicketRuleOverlay"


def rule_document() -> dict[str, JsonValue]:
    """The whole overlay document with every key the reader accepts."""
    return {
        "overlay_version": "1.0.0",
        "ticket": "OMN-1",
        "ruling": "fixture ruling",
        "endpoint": "https://linear.invalid/graphql",
        "cache_seconds": 60,
        "reads_per_tick": 4,
        "read_timeout_seconds": 5,
        "max_depth": 6,
        "ticket_roots": ["OMN-1", "OMN-2"],
    }


def rule_record() -> dict[str, Any]:
    document = rule_document()
    return ModelOverlayStoreRecord(
        overlay_id=OVERLAY_ID,
        digest=document_digest(document),
        document=document,
        source_ref=f"{OVERLAY_ID}@abc123",
        loaded_by="loader",
        loaded_at=LIVE_LOADED_AT,
    ).model_dump(mode="json")


def request(value: JsonValue) -> ModelOverlaySettingSetRequest:
    return ModelOverlaySettingSetRequest.model_validate(
        {"overlay_id": OVERLAY_ID, "key": KEY, "value": value, "actor": ACTOR}
    )


def test_the_contract_declares_the_ticket_roots_row() -> None:
    rows = [
        row
        for row in contract()["settable_settings"]
        if row["overlay_id"] == OVERLAY_ID
    ]
    assert [(row["key"], row["path"], row["document_model"]) for row in rows] == [
        (KEY, KEY, MODEL)
    ]


async def test_a_valid_list_of_tickets_is_set_and_stored() -> None:
    handler, bus, _ = build({OVERLAY_ID: rule_record()})
    value: JsonValue = ["OMN-1", "OMN-2", "OMN-30"]
    result = await handler.handle(request(value))
    assert result.setting_path == KEY
    stored = [json.loads(v) for _topic, _key, v in bus.published]
    records = [row for row in stored if row.get("overlay_id") == OVERLAY_ID]
    assert records[0]["document"]["ticket_roots"] == value
    assert records[0]["operator_paths"] == [KEY]


@pytest.mark.parametrize(
    "value",
    [
        ["not-a-ticket"],
        ["OMN-1", "omn-2"],
        ["OMN-1 "],
        [],
        "OMN-1",
        {"OMN-1": True},
        ["OMN-1", 2],
        [True],
        None,
        7,
    ],
)
async def test_an_invalid_ticket_roots_value_is_refused_and_nothing_is_written(
    value: JsonValue,
) -> None:
    handler, bus, _ = build({OVERLAY_ID: rule_record()})
    with pytest.raises(SettingValueInvalidError, match="ticket_roots"):
        await handler.handle(request(value))
    assert bus.published == []
