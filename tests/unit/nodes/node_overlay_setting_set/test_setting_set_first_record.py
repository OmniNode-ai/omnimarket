# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A set on a declared overlay the store holds no record of creates its first record, validated by the overlay's own model."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    OverlayNotInStoreError,
    SettingNotDeclaredError,
    SettingValueInvalidError,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import ACTOR, NOW, build

pytestmark = pytest.mark.unit

RESPONDERS = [
    {"tier": "local", "base_url": "http://127.0.0.1:8000"},
    {"tier": "openrouter"},
]
GATEWAY = {
    "schema_version": 1,
    "gateway_url": "http://gateway.example.test:28085",
    "tenant_slug": "dev-tenant",
    "credential_kind": "api_key",
}


def req(overlay: str, key: str, value: object) -> ModelOverlaySettingSetRequest:
    return ModelOverlaySettingSetRequest.model_validate(
        {"overlay_id": overlay, "key": key, "value": value, "actor": ACTOR}
    )


async def test_responders_first_record_is_created_with_provenance() -> None:
    handler, bus, _ = build(live={})
    result = await handler.handle(
        req("delegation_check_responders", "responders", RESPONDERS)
    )
    assert result.previous_present is False
    assert result.set_at == NOW
    ((_, record_raw),) = bus.on(result.loaded_topic)
    record = json.loads(record_raw)
    assert record["document"] == {"schema_version": 1, "responders": RESPONDERS}
    assert record["loaded_by"] == ACTOR
    assert record["operator_paths"] == ["responders"]
    assert len(bus.on(result.audit_topic)) == 1


async def test_second_set_builds_on_the_first_record() -> None:
    handler, _, _ = build(live={})
    await handler.handle(req("delegation_check_responders", "responders", RESPONDERS))
    second = await handler.handle(
        req("delegation_check_responders", "responders", [{"tier": "claude_api"}])
    )
    assert second.previous_present is True
    assert second.previous_value == RESPONDERS


async def test_gateway_first_record_is_the_whole_document() -> None:
    handler, bus, _ = build(live={})
    result = await handler.handle(req("delegation_check_gateway", "gateway", GATEWAY))
    ((_, raw),) = bus.on(result.loaded_topic)
    record = json.loads(raw)
    assert record["document"] == GATEWAY
    assert record["loaded_by"] == ACTOR
    assert sorted(record["operator_paths"]) == sorted(GATEWAY)


async def test_invalid_first_document_is_refused_with_nothing_written() -> None:
    handler, bus, _ = build(live={})
    with pytest.raises(SettingValueInvalidError):
        await handler.handle(
            req(
                "delegation_check_responders",
                "responders",
                [{"tier": "local", "base_url": "http://8.8.8.8:80"}],
            )
        )
    with pytest.raises(SettingValueInvalidError):
        await handler.handle(
            req(
                "delegation_check_gateway",
                "gateway",
                {**GATEWAY, "gateway_url": "http://localhost:1"},
            )
        )
    assert bus.published == []


async def test_undeclared_overlay_is_still_refused() -> None:
    handler, bus, _ = build(live={})
    with pytest.raises(SettingNotDeclaredError):
        await handler.handle(req("not_a_declared_overlay", "responders", RESPONDERS))
    assert bus.published == []


async def test_overlay_declaring_no_first_document_still_refuses_an_empty_store() -> (
    None
):
    handler, bus, _ = build(live={})
    with pytest.raises(OverlayNotInStoreError):
        await handler.handle(req("goal_scope_ticket_rule", "ticket_roots", ["OMN-1"]))
    assert bus.published == []


async def test_claude_tier_first_record_holds_the_tier_row() -> None:
    """The first set of a Claude tier row on a store with no record creates the document, not a missing-parent refusal."""
    row = {"credential_order": ["subscription"]}
    handler, bus, _ = build(live={})
    result = await handler.handle(
        req("claude_harness_tiers", "tiers.harness-claude-sonnet", row)
    )
    assert result.previous_present is False
    ((_, raw),) = bus.on(result.loaded_topic)
    document = json.loads(raw)["document"]
    assert document == {"schema_version": 1, "tiers": {"harness-claude-sonnet": row}}
