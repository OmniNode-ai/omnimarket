# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the deploy agent key check effect, with the golden vector (OMN-24413).

* golden: the overlay's variable holds the agent's key -> served, with the check value of the golden vector;
* error: the variable is unset or empty -> refused with a typed detail, and the key text is in no result field;
* control: a handler that returned the key itself would fail the no-key assertion (shown by the injected break test).
"""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_deploy_agent_key_check_effect.handlers.handler_deploy_agent_key_check import (
    HandlerDeployAgentKeyCheck,
)
from omnimarket.nodes.node_deploy_agent_key_check_effect.models.model_deploy_agent_key_check import (
    key_check_value,
)
from tests.nodes.node_deploy_agent_key_check_effect.support import (
    AGENT_KEY,
    AGENT_KEY_ENV,
    RUN_ID,
    VECTOR,
    overlay,
    request,
)

pytestmark = pytest.mark.unit


async def test_golden_chain_serves_the_golden_check_value() -> None:
    handler = HandlerDeployAgentKeyCheck(
        overlay=overlay(), environ={AGENT_KEY_ENV: AGENT_KEY}
    )
    result = await handler.handle(request())

    vector = json.loads(VECTOR.read_text())
    assert vector["secret"] == AGENT_KEY
    assert result.status == "served"
    assert result.correlation_id == RUN_ID
    assert result.key_check == vector["check"] == "6dce9bc99db04417"
    assert result.key_check == key_check_value(AGENT_KEY)


@pytest.mark.parametrize("environ", [{}, {AGENT_KEY_ENV: ""}], ids=["unset", "empty"])
async def test_error_chain_refuses_when_the_secret_is_not_set(
    environ: dict[str, str],
) -> None:
    handler = HandlerDeployAgentKeyCheck(overlay=overlay(), environ=environ)
    result = await handler.handle(request())

    assert result.status == "refused"
    assert result.key_check == ""
    assert "not set" in result.detail


async def test_the_key_is_in_no_field_of_the_result() -> None:
    handler = HandlerDeployAgentKeyCheck(
        overlay=overlay(), environ={AGENT_KEY_ENV: AGENT_KEY}
    )
    served = await handler.handle(request())
    assert AGENT_KEY not in served.model_dump_json()

    refused = await HandlerDeployAgentKeyCheck(overlay=overlay(), environ={}).handle(
        request()
    )
    assert AGENT_KEY not in refused.model_dump_json()


async def test_another_key_gives_another_check_value() -> None:
    handler = HandlerDeployAgentKeyCheck(
        overlay=overlay(), environ={AGENT_KEY_ENV: "another-fixture-key"}
    )
    result = await handler.handle(request())
    assert result.status == "served"
    assert result.key_check != "6dce9bc99db04417"
    assert len(result.key_check) == 16


async def test_detail_is_cut_to_the_overlay_bound() -> None:
    bounded = overlay().model_copy(update={"detail_max_chars": 10})
    result = await HandlerDeployAgentKeyCheck(overlay=bounded, environ={}).handle(
        request()
    )
    assert len(result.detail) == 10
