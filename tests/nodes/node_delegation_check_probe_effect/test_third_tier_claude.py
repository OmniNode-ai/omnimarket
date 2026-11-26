# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The third tier of the delegation responder chain is Claude, from the bootstrap overlay and from the settings store.

Operator ruling 2026-11-26T13:43:36Z: the third tier is Claude (the delegation feature's Claude harness handler on the
subscription, never an API key, never Gemini), declared first in the delegation routing overlay for bootstrap and then as a
settings store row the runtime reads after bootstrap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelResponderReading,
    resolve_chain,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
    ModelDelegationCheckRespondersOverlay,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_serving_tier,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import ACTOR, build

FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "delegation_check_responders"
    / "overlay.yaml"
)
ENV = "ONEX_DELEGATION_CHECK_RESPONDERS_OVERLAY"


def _none_answers(base_url: str) -> ModelResponderReading:
    return ModelResponderReading(base_url=base_url, answered=False)


def test_the_bootstrap_overlay_declares_claude_as_the_third_tier() -> None:
    row = load_structured_overlay(
        "delegation_check_responders",
        ModelDelegationCheckRespondersOverlay,
        {ENV: str(FIXTURE)},
        file_env=ENV,
    )
    assert [r.tier for r in row.responders] == ["local", "openrouter", "claude"]
    assert resolve_chain(row.responders, _none_answers).attempts[-1].tier == "claude"


async def test_the_settings_store_row_resolves_its_third_tier_to_claude() -> None:
    handler, bus, _ = build(live={})
    value = [
        {"tier": "local", "base_url": "http://127.0.0.12:8000"},
        {"tier": "openrouter"},
        {"tier": "claude"},
    ]
    result = await handler.handle(
        ModelOverlaySettingSetRequest.model_validate(
            {
                "overlay_id": "delegation_check_responders",
                "key": "responders",
                "value": value,
                "actor": ACTOR,
            }
        )
    )
    ((_, raw),) = bus.on(result.loaded_topic)
    document = json.loads(raw)["document"]
    row = ModelDelegationCheckRespondersOverlay.model_validate(document)
    assert [r.tier for r in row.responders][2] == "claude"
    assert resolve_chain(row.responders, _none_answers).attempts[-1].tier == "claude"


def test_the_claude_api_tier_is_gone() -> None:
    with pytest.raises(ValidationError):
        ModelDelegationCheckResponder.model_validate({"tier": "claude_api"})


def test_the_claude_tier_serves_as_the_claude_harness_not_gemini() -> None:
    # Known good: the Claude harness backends of the delegation routing overlay declare provider anthropic.
    assert grade_serving_tier("anthropic", "claude").ok
    # Known bad: the Gemini mapping of the previous commit, and the other tiers' words.
    assert not grade_serving_tier("gemini", "claude").ok
    assert not grade_serving_tier("claude_api", "claude").ok
    assert not grade_serving_tier("openrouter", "claude").ok
    assert not grade_serving_tier("anthropic", "openrouter").ok
