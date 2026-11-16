# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_code_contract_score_effect (OMN-23626)."""

from __future__ import annotations

import importlib.resources

import pytest
import yaml

from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreRuntime,
    EnumCodeScoreVerdict,
    ModelCodeScored,
)
from tests.nodes.node_code_contract_score_effect.fixture_repo import (
    Fixture,
    request,
)

pytestmark = pytest.mark.unit


class FakeBus:
    """A bus that records publishes and never raises (unless told to)."""

    def __init__(self, raise_on_publish: bool = False) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []
        self._raise_on_publish = raise_on_publish

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        if self._raise_on_publish:
            raise RuntimeError("bus publish failed")
        self.published.append((topic, key, value))


def contract() -> dict:
    """Load the contract YAML for the node."""
    files = importlib.resources.files(
        "omnimarket.nodes.node_code_contract_score_effect"
    )
    path = files.joinpath("contract.yaml")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def test_contract_declares_the_topics() -> None:
    c = contract()
    assert c["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.code-contract-score-requested.v1"
    ]
    assert c["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.code-contract-scored.v1",
        "onex.evt.omnimarket.code-contract-score-failed.v1",
    ]
    assert c["node_type"] == "EFFECT_GENERIC"


async def test_a_request_yields_one_scored_event(fx: Fixture) -> None:
    bus = FakeBus()
    handler = HandlerCodeContractScore(event_bus=bus)
    result = await handler.handle(request(fx, fx.good_diff))
    assert len(bus.published) == 1
    topic, key, value = bus.published[0]
    assert topic == "onex.evt.omnimarket.code-contract-scored.v1"
    assert key == str(request(fx, fx.good_diff).correlation_id).encode()
    scored = ModelCodeScored.model_validate_json(value)
    assert scored == result.scored
    assert result.failed is None
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.PASS
    assert result.scored.verdict.runtime is EnumCodeScoreRuntime.UNREAD_EVAL


async def test_bus_failure_never_changes_the_verdict(fx: Fixture) -> None:
    bus = FakeBus(raise_on_publish=True)
    handler = HandlerCodeContractScore(event_bus=bus)
    result = await handler.handle(request(fx, fx.good_diff))
    assert result.scored is not None
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.PASS
