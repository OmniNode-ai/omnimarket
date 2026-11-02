# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a gate attempt's voter result file reaches a decision on the step files.

The chain runs node_agent_review_gate_accept_effect over real files (the production file
adapter) with the review and verifier ports faked: a degraded attempt whose voters were
lost to exhausted credit and a timeout, with a receipt the verifier accepts, ends
``pass_agent`` with ``decision`` in the step output and the causes in the step summary.
The error leg: an attempt with no result file is refused, and a node built with no
ports bound refuses an eligible attempt instead of passing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_agent_review_gate_accept_effect import (
    NodeAgentReviewGateAcceptEffect,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.adapters.adapter_gate_accept_ports import (
    LocalStepFiles,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptDecision,
    EnumGateAcceptReason,
    ModelGateAcceptRequest,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import (
    load_gate_accept_policy,
)
from tests.node_overlay_support import install_node_overlay
from tests.unit.nodes.node_agent_review_gate_accept_effect.support import (
    CREDIT_ERROR,
    HEAD,
    PR,
    PUBLISHER,
    REPO,
    TIMEOUT_ERROR,
    VERIFIER_ENDPOINT,
    FakeReviews,
    FakeVerifier,
    accepted,
    degraded_with,
    receipt_review,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _overlay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path / "overlay-root",
        "agent_review_gate_accept",
        {"publisher_login": PUBLISHER, "verifier_endpoint": VERIFIER_ENDPOINT},
    )


def _request(tmp_path: Path, result: str | None) -> ModelGateAcceptRequest:
    result_path = tmp_path / "hostile-reviewer-result.json"
    if result is not None:
        result_path.write_text(result, encoding="utf-8")
    return ModelGateAcceptRequest(
        repository=REPO,
        pull_request=PR,
        head_sha=HEAD,
        voter_result_path=str(result_path),
        step_output_path=str(tmp_path / "github-output"),
        step_summary_path=str(tmp_path / "github-summary"),
    )


def test_a_degraded_attempt_with_an_accepted_receipt_passes_agent(
    tmp_path: Path,
) -> None:
    node = NodeAgentReviewGateAcceptEffect(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([receipt_review("rcpt-1")]),
        verifier=FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
        files=LocalStepFiles(),
    )
    request = _request(tmp_path, degraded_with(CREDIT_ERROR, TIMEOUT_ERROR))
    result = node.handle(request)
    assert result.decision is EnumGateAcceptDecision.PASS_AGENT
    output = Path(request.step_output_path).read_text(encoding="utf-8").splitlines()
    assert output == [
        "decision=pass_agent",
        "reason=receipt_accepted",
        "receipt_id=rcpt-1",
    ]
    summary = Path(request.step_summary_path).read_text(encoding="utf-8")
    assert "voter-1=credit_exhausted" in summary
    assert "voter-2=timeout" in summary
    assert "source=agent receipt=rcpt-1" in summary


def test_an_attempt_with_no_result_file_is_refused_on_the_step_files(
    tmp_path: Path,
) -> None:
    node = NodeAgentReviewGateAcceptEffect(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([]),
        verifier=FakeVerifier(),
        files=LocalStepFiles(),
    )
    request = _request(tmp_path, None)
    result = node.handle(request)
    assert result.decision is EnumGateAcceptDecision.REFUSE
    assert result.reason is EnumGateAcceptReason.VOTER_RESULT_UNREADABLE
    output = Path(request.step_output_path).read_text(encoding="utf-8")
    assert "decision=refuse" in output.splitlines()


def test_a_node_with_no_bound_ports_refuses_an_eligible_attempt(
    tmp_path: Path,
) -> None:
    node = NodeAgentReviewGateAcceptEffect()
    result = node.handle(_request(tmp_path, degraded_with(CREDIT_ERROR, CREDIT_ERROR)))
    assert result.decision is EnumGateAcceptDecision.REFUSE
    assert result.reason is EnumGateAcceptReason.REVIEWS_UNREADABLE
