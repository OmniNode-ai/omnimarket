# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent path reaches the CI gate's verdict on three recorded fixtures.

Each fixture holds the recorded voter outputs and the verdict the CI gate recorded for the
same outputs (the gate counts a vote per distinct endpoint plus model, needs a quorum of
at least two, and blocks on a finding raised by at least two distinct voters). The outputs
go through the node's aggregation; the verdicts must be equal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers.handler_agent_review_quorum import (
    HandlerAgentReviewQuorum,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.overlay import (
    load_agent_review_config,
)

from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    ModelAgentQuorumRequest,
)
from tests.node_overlay_support import install_node_overlay

from .support import NODE, OVERLAY

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent / "fixtures" / "parity_fixtures.json"


def _cases() -> list[dict[str, Any]]:
    return json.loads(_FIXTURES.read_text("utf-8"))["fixtures"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_the_agent_verdict_equals_the_ci_gate_verdict(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE, OVERLAY)
    config = load_agent_review_config()
    result = HandlerAgentReviewQuorum().handle(
        ModelAgentQuorumRequest(
            roster=config.roster, reviews=case["voter_outputs"], quorum=config.quorum
        )
    )
    assert result.sufficient is True
    assert result.verdict == case["ci_gate_verdict"]
    assert len(result.blocking) == case["ci_gate_blocking_count"]


def test_the_three_fixtures_cover_clean_one_voter_and_two_voter() -> None:
    assert [c["name"] for c in _cases()] == [
        "clean-diff",
        "one-voter-finding",
        "two-voter-finding",
    ]
