# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only explicit degraded verdicts may reach the gate's receipt path."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from omnimarket.nodes.node_agent_review_gate_accept_effect import (
    NodeAgentReviewGateAcceptEffect,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptDecision as Decision,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptReason as Reason,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    ModelGateAcceptPolicy,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import (
    load_gate_accept_policy,
)

from .conftest import Gate, request_for
from .support import (
    CREDIT_ERROR,
    OUTPUT_PATH,
    SUMMARY_PATH,
    FakeVerifier,
    accepted,
    degraded_with,
    receipt_review,
    result_file,
    voter,
)

MakeGate = Callable[..., Gate]


@pytest.mark.parametrize(
    "verdict",
    [
        "approved",
        "PASSED",
        "degraded_quorum_typo",
        "unknown",
        pytest.param("x" * 5000, id="long-verdict"),
        pytest.param("degraded\nblocked", id="newline-verdict"),
        "Degraded",
        " passed",
    ],
)
async def test_unknown_verdict_is_refused(make_gate: MakeGate, verdict: str) -> None:
    document: dict[str, object] = json.loads(degraded_with(CREDIT_ERROR, CREDIT_ERROR))
    document["quorum"] = {"verdict": verdict}
    gate = make_gate(
        json.dumps(document),
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason("verdict_unrecognised")
    assert gate.verifier.calls == []
    assert gate.reviews.reads == []
    assert "\n" not in result.summary_line
    assert "\r" not in result.summary_line
    assert len(result.summary_line) < 400
    assert "x" * 81 not in result.summary_line
    if verdict == "approved":
        assert "approved" in result.summary_line


@pytest.mark.parametrize(
    ("quorum", "succeeded"),
    [
        pytest.param(None, True, id="no-quorum-with-succeeded-voters"),
        pytest.param(None, False, id="no-quorum-with-lost-voters"),
        pytest.param({}, False, id="no-verdict-key"),
        pytest.param({"verdict": ""}, False, id="empty-verdict"),
    ],
)
async def test_missing_verdict_is_refused(
    make_gate: MakeGate, quorum: dict[str, str] | None, succeeded: bool
) -> None:
    error = None if succeeded else CREDIT_ERROR
    document: dict[str, object] = json.loads(
        result_file(
            voter("voter-1", error=error), voter("voter-2", error=error), verdict=None
        )
    )
    if quorum is not None:
        document["quorum"] = quorum
    gate = make_gate(
        json.dumps(document),
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason("verdict_unread")
    assert gate.verifier.calls == []
    assert gate.reviews.reads == []


@pytest.mark.parametrize(
    "quorum",
    [
        pytest.param({"verdict": 7}, id="integer-verdict"),
        pytest.param({"verdict": True}, id="boolean-verdict"),
        pytest.param({"verdict": None}, id="null-verdict"),
        pytest.param({"verdict": ["degraded"]}, id="list-verdict"),
        pytest.param({"verdict": {"verdict": "degraded"}}, id="object-verdict"),
        pytest.param("degraded", id="string-quorum"),
        pytest.param(["degraded"], id="list-quorum"),
    ],
)
async def test_malformed_verdict_is_refused(
    make_gate: MakeGate, quorum: object
) -> None:
    document: dict[str, object] = json.loads(degraded_with(CREDIT_ERROR, CREDIT_ERROR))
    document["quorum"] = quorum
    gate = make_gate(
        json.dumps(document),
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason("verdict_unread")
    assert gate.verifier.calls == []
    assert gate.reviews.reads == []


@pytest.mark.parametrize(
    "verdict",
    # Keep collection possible before the field exists; fail in the test instead.
    ModelGateAcceptPolicy.model_fields["degraded_verdicts"].default
    if "degraded_verdicts" in ModelGateAcceptPolicy.model_fields
    else [pytest.param(None, id="missing-degraded-verdicts-field")],
)
async def test_every_default_degraded_verdict_reaches_the_receipt_path(
    make_gate: MakeGate, verdict: str | None
) -> None:
    defaults = ModelGateAcceptPolicy.model_fields["degraded_verdicts"].default
    assert isinstance(verdict, str)
    assert verdict in defaults
    gate = make_gate(
        result_file(
            voter("voter-1", error=CREDIT_ERROR),
            voter("voter-2", error=CREDIT_ERROR),
            verdict=verdict,
        ),
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.PASS_AGENT
    assert result.reason is Reason.RECEIPT_ACCEPTED
    assert len(gate.verifier.calls) == 1


async def test_no_models_is_in_the_default_degraded_set() -> None:
    defaults = ModelGateAcceptPolicy.model_fields["degraded_verdicts"].default
    assert isinstance(defaults, tuple)
    assert all(isinstance(verdict, str) for verdict in defaults)
    assert "no_models" in defaults
    assert "degraded" in defaults
    assert "degraded_quorum" in defaults


async def test_the_loaded_policy_uses_the_default_degraded_set() -> None:
    field_name = "degraded_verdicts"
    defaults = ModelGateAcceptPolicy.model_fields[field_name].default
    assert getattr(load_gate_accept_policy(), field_name) == defaults


@pytest.mark.parametrize(
    ("verdict", "decision", "reason"),
    [
        ("custom_outage", Decision.PASS_AGENT, "receipt_accepted"),
        ("degraded_quorum", Decision.REFUSE, "verdict_unrecognised"),
    ],
)
async def test_the_degraded_set_is_replaceable_through_the_policy(
    make_gate: MakeGate, verdict: str, decision: Decision, reason: str
) -> None:
    gate = make_gate(
        result_file(
            voter("voter-1", error=CREDIT_ERROR),
            voter("voter-2", error=CREDIT_ERROR),
            verdict=verdict,
        ),
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    policy = load_gate_accept_policy().model_copy(
        update={"degraded_verdicts": ("custom_outage",)}
    )
    node = NodeAgentReviewGateAcceptEffect(
        policy=policy, reviews=gate.reviews, verifier=gate.verifier, files=gate.files
    )
    result = node.handle(request_for())
    assert result.decision is decision
    assert result.reason is Reason(reason)
    if decision is Decision.PASS_AGENT:
        assert len(gate.verifier.calls) == 1
    else:
        assert gate.verifier.calls == []
        assert gate.reviews.reads == []


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [("approved", "verdict_unrecognised"), (None, "verdict_unread")],
)
async def test_a_verdict_refusal_writes_decision_and_reason_to_the_step_files(
    make_gate: MakeGate, verdict: str | None, reason: str
) -> None:
    gate = make_gate(
        result_file(
            voter("voter-1", error=CREDIT_ERROR),
            voter("voter-2", error=CREDIT_ERROR),
            verdict=verdict,
        ),
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason(reason)
    assert gate.verifier.calls == []
    assert gate.reviews.reads == []
    output = gate.files.files[OUTPUT_PATH].splitlines()
    assert "decision=refuse" in output
    assert f"reason={reason}" in output
    assert result.summary_line in gate.files.files[SUMMARY_PATH]


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [("passed", Reason.VOTERS_PASSED), ("blocked", Reason.CI_BLOCKED)],
)
async def test_a_blocked_or_passed_verdict_still_stands(
    make_gate: MakeGate, verdict: str, reason: Reason
) -> None:
    gate = make_gate(
        result_file(
            voter("voter-1", error=CREDIT_ERROR),
            voter("voter-2", error=CREDIT_ERROR),
            verdict=verdict,
        ),
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.UNCHANGED
    assert result.reason is reason
    assert gate.verifier.calls == []
    assert gate.reviews.reads == []
