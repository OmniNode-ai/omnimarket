# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Determinism of node_code_contract_score_effect (OMN-23626)."""

from __future__ import annotations

import hashlib

import pytest

from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreFailureCode,
    EnumCodeScoreVerdict,
)
from tests.nodes.node_code_contract_score_effect.fixture_repo import (
    Fixture,
    request,
)

pytestmark = pytest.mark.unit


class FakeBus:
    """A bus that records publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.published.append((topic, key, value))


def project(verdict) -> tuple:
    """Project a verdict to a comparable tuple."""
    return (
        verdict.checks,
        verdict.source,
        tuple((r.item_id, r.outcome, r.exit_code) for r in verdict.check_results),
    )


async def test_same_request_three_times_same_verdict(fx: Fixture) -> None:
    projections: list[tuple] = []
    for _ in range(3):
        handler = HandlerCodeContractScore()
        result = await handler.handle(request(fx, fx.good_diff))
        assert result.scored is not None
        projections.append(project(result.scored.verdict))
    assert projections[0] == projections[1] == projections[2]
    assert projections[0][0] is EnumCodeScoreVerdict.PASS

    projections: list[tuple] = []
    for _ in range(3):
        handler = HandlerCodeContractScore()
        result = await handler.handle(request(fx, ""))
        assert result.scored is not None
        projections.append(project(result.scored.verdict))
    assert projections[0] == projections[1] == projections[2]
    assert projections[0][0] is EnumCodeScoreVerdict.FAIL_CHECK


async def test_check_file_with_a_different_sha256_is_refused(fx: Fixture) -> None:
    bus = FakeBus()
    handler = HandlerCodeContractScore(event_bus=bus)
    result = await handler.handle(request(fx, fx.good_diff, check_sha="0" * 64))
    assert result.scored is None
    assert result.failed.code is EnumCodeScoreFailureCode.CHECK_FILE_SHA256_MISMATCH
    assert len(bus.published) == 1
    assert bus.published[0][0] == "onex.evt.omnimarket.code-contract-score-failed.v1"


async def test_contract_with_a_different_sha256_is_refused(fx: Fixture) -> None:
    bus = FakeBus()
    handler = HandlerCodeContractScore(event_bus=bus)
    result = await handler.handle(request(fx, fx.good_diff, contract_sha="0" * 64))
    assert result.scored is None
    assert result.failed.code is EnumCodeScoreFailureCode.CONTRACT_SHA256_MISMATCH
    assert len(bus.published) == 1
    assert bus.published[0][0] == "onex.evt.omnimarket.code-contract-score-failed.v1"


async def test_draft_diff_hash_is_the_sha256_of_the_diff(fx: Fixture) -> None:
    handler = HandlerCodeContractScore()
    result = await handler.handle(request(fx, fx.good_diff))
    assert result.scored is not None
    expected = hashlib.sha256(fx.good_diff.encode()).hexdigest()
    assert result.scored.draft_diff_sha256 == expected
