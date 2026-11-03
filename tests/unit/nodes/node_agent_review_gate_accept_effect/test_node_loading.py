# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate-accept node loads through its contract, overlay and injected ports.

Run in both profiles (see ``conftest``). Deployment facts come from the overlay; the
rules (cause patterns, severities, fence) come from the contract; the handler holds none.
"""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_agent_review_gate_accept_effect import (
    NodeAgentReviewGateAcceptEffect,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.handlers import (
    handler_gate_accept,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptDecision as Decision,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptReason as Reason,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    ModelGateAcceptTopics,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import (
    load_contract,
    load_gate_accept_policy,
    load_topics,
)
from tests.node_overlay_support import install_node_overlay

from .conftest import Gate, Profile, request_for
from .support import (
    CREDIT_ERROR,
    PUBLISHER,
    VERIFIER_ENDPOINT,
    FakeFiles,
    FakeReviews,
    FakeVerifier,
    accepted,
    degraded_with,
    receipt_review,
)

MakeGate = Callable[..., Gate]


def test_contract_topics() -> None:
    assert load_topics() == ModelGateAcceptTopics(
        command="onex.cmd.omnimarket.agent-review-gate-accept-requested.v1",
        decided="onex.evt.omnimarket.agent-review-gate-accept-decided.v1",
    )


def test_contract_binds_the_handler_and_the_declared_output_fields() -> None:
    contract = load_contract()
    assert contract["handler"]["class"] == "HandlerGateAccept"
    assert set(contract["outputs"]) == {
        "decision",
        "reason",
        "receipt_id",
        "summary_line",
    }
    assert contract["terminal_event"] == load_topics().decided


def test_the_node_is_registered_as_an_entry_point() -> None:
    names = {
        entry.name for entry in importlib.metadata.entry_points(group="onex.nodes")
    }
    assert "node_agent_review_gate_accept_effect" in names


def test_the_overlay_supplies_the_publisher_and_the_verifier_endpoint() -> None:
    policy = load_gate_accept_policy()
    assert policy.publisher_login == PUBLISHER
    assert policy.verifier_endpoint == VERIFIER_ENDPOINT


def test_no_overlay_refuses_instead_of_guessing_an_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    with pytest.raises(NodeOverlayError):
        load_gate_accept_policy()


async def test_policy_values_are_data_the_node_follows(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        profile.root,
        "agent_review_gate_accept",
        {
            "publisher_login": "another-app[bot]",
            "verifier_endpoint": "http://other-verifier.test:9",
            "transport": profile.transport.value,
        },
    )
    lost = degraded_with(CREDIT_ERROR, CREDIT_ERROR)
    old = Gate(
        profile,
        FakeFiles({"/runner/temp/hostile-reviewer-result.json": lost}),
        FakeReviews([receipt_review("rcpt-1")]),
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    assert (await old.decide(request_for())).decision is Decision.UNCHANGED
    new = Gate(
        profile,
        FakeFiles({"/runner/temp/hostile-reviewer-result.json": lost}),
        FakeReviews([receipt_review("rcpt-1", author="another-app[bot]")]),
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    assert (await new.decide(request_for())).decision is Decision.PASS_AGENT
    assert new.verifier.calls[0].verifier_endpoint == "http://other-verifier.test:9"


def test_the_node_class_runs_the_handler_with_the_given_ports() -> None:
    node = NodeAgentReviewGateAcceptEffect(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([]),
        verifier=FakeVerifier(),
        files=FakeFiles(),
    )
    assert isinstance(node, handler_gate_accept.HandlerGateAccept)


def test_the_handler_holds_no_operational_literal() -> None:
    source = Path(handler_gate_accept.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for literal in ("onex-review-receipt", "ONEX_CORE_", "http://", "https://"):
        assert literal not in code, literal
    assert not re.search(r"\[[\"'](critical|error)[\"']", code)


def test_the_cause_patterns_live_in_the_contract() -> None:
    causes = load_contract()["config"]["agent_review_gate_accept"]["causes"]
    assert set(causes) == {"credit_exhausted", "timeout", "unavailable"}
    for patterns in causes.values():
        for pattern in patterns:
            re.compile(pattern)
    assert yaml.safe_dump(causes)


async def test_a_node_with_no_overlay_refuses_the_request_and_names_why(
    make_gate: Callable[..., Gate], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = make_gate(degraded_with(CREDIT_ERROR, CREDIT_ERROR))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    bare = NodeAgentReviewGateAcceptEffect(
        reviews=gate.reviews, verifier=gate.verifier, files=gate.files
    )
    result = bare.handle(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason.POLICY_UNAVAILABLE
    assert gate.verifier.calls == []
