# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J14 (piece 14 to piece 15): a signed receipt carries a lab-model voter result (OMN-22975).

Rebuild plan 4.1, J14: piece 14's signed receipt for a head carries a lab-model voter result obtained through the
chain of responders, and an unreachable rung is typed.

The join is run through the two real handlers that own it, with only their ports faked. Piece 14's review dispatch
(``HandlerAgentReviewDispatch``) calls the roster's rungs one at a time in the overlay's order, signs the receipt over
the votes it gathered and posts it as a pull request review. The hosted gate (``HandlerGateAccept``) then reads that
posted review for the exact head and asks a verifier whether the signature holds.

Golden: the lab rung answers, so the stored, signed receipt names the lab-model voters (endpoint class, model and the
delegation event that proves each vote); the review the dispatch posted is read by the gate for the same head and the
verifier accepts it. When a first rung is unreachable the lab rung still answers and the receipt names only the voters
that answered, while its roster snapshot keeps the whole roster.

Error: an unreachable rung is typed on the command's terminal, the record the dispatch keeps of the chain, as
``unavailable`` or ``timeout`` with the exception type as its cause, never as a red of its own. When no rung can answer,
the terminal is refused with every attempt typed, no receipt is signed or posted, and the gate finds no receipt and refuses
on its wait rather than inventing a voter result.

What this reads as "typed in the receipt": the signed receipt itself has no field for an attempt; the dispatch's
terminal carries the attempts, so the error assertions are on that terminal and on what the receipt omits.

Two injected breaks show the assertions can fail for real: a receipt whose lab voter was dropped after signing, and a
receipt read for another head. The ``test_control_*`` tests pin both.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_agent_review_dispatch_orchestrator.adapters.adapter_agent_review_ports import (
    render_receipt_review,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    EnumAgentReviewOutcome,
    EnumAgentReviewReason,
    EnumAgentRungOutcome,
    ModelAgentReceipt,
    ModelAgentReceiptCandidate,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.handlers.handler_gate_accept import (
    HandlerGateAccept,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptDecision,
    EnumGateAcceptReason,
    ModelReceiptGateRequest,
    ModelReceiptVerifyOutcome,
    ModelReceiptVerifyRequest,
    ModelReviewView,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import (
    load_gate_accept_policy,
)
from tests.node_overlay_support import install_node_overlay
from tests.unit.nodes.node_agent_review_dispatch_orchestrator.support import (
    HEAD,
    KEY,
    MOVED,
    OVERLAY,
    PR,
    REPO,
    Harness,
    World,
    build_harness,
)
from tests.unit.nodes.node_agent_review_gate_accept_effect.support import (
    FakeFiles,
    FakeReviews,
)

pytestmark = pytest.mark.unit

FENCE = "onex-review-receipt"
PUBLISHER = "test-publisher"
OUTPUT_PATH = "/runner/temp/github-output"
SUMMARY_PATH = "/runner/temp/github-summary"
LAB_CLASS = "lab_local"
#: The recorded gateway reply of a credit-exhausted rung, as the lab call renders it.
RECORDED_CREDIT_503 = (
    "[ONEX_CORE_205_SERVICE_UNAVAILABLE] Server error (503) from review-gateway"
    ' :: response_body={"error":{"type":"gateway_keys_set_aside",'
    '"reason":"credit_exhausted","message":"every key was refused"}}'
)


def _voter(identity: str, rung: str, endpoint_class: str, host: int) -> dict[str, str]:
    return {
        "identity": identity,
        "provider": "test-provider",
        "rung": rung,
        "endpoint_class": endpoint_class,
        "endpoint": f"192.0.2.{host}:8000",
        "model": f"model-{identity}",
        "route_key": f"route-{identity}",
    }


def chain_overlay(order: tuple[str, ...]) -> dict[str, Any]:
    """Two lab-model voters on the ``lab`` rung and one harness voter on the ``harness`` rung."""
    fields = copy.deepcopy(OVERLAY)
    fields["roster"] = [
        _voter("lab-a", "lab", LAB_CLASS, 31),
        _voter("lab-b", "lab", LAB_CLASS, 32),
        _voter("harness-a", "harness", "harness", 33),
    ]
    fields["rung_order"] = list(order)
    fields["receipt_fence"] = FENCE
    fields["policy"]["approved_identities"] = [v["identity"] for v in fields["roster"]]
    fields["policy"]["approved_rungs"] = ["lab", "harness"]
    fields["policy"]["forbidden_providers"] = []
    fields["policy"]["publisher_identity"] = PUBLISHER
    fields["lab_call_timeout_seconds"] = 0.05
    return fields


class SignatureVerifier:
    """The verifier port: accepts a receipt whose signature is the one the fake signer issues,
    for the head the gate runs on, and refuses anything else with a typed reason."""

    def __init__(self) -> None:
        self.calls: list[ModelReceiptVerifyRequest] = []

    def verify(self, request: ModelReceiptVerifyRequest) -> ModelReceiptVerifyOutcome:
        self.calls.append(request)
        receipt = request.receipt
        receipt_id = str(receipt.get("id", ""))
        if receipt.get("head_sha") != request.head_sha:
            return ModelReceiptVerifyOutcome(accepted=False, reason="head_mismatch")
        signature = receipt.get("signature", "")
        candidate = ModelAgentReceiptCandidate.model_validate(
            {k: v for k, v in receipt.items() if k != "signature"}
        )
        digest = hashlib.sha256(candidate.model_dump_json().encode()).hexdigest()
        if signature != digest:
            return ModelReceiptVerifyOutcome(accepted=False, reason="signature_invalid")
        if not any(v.endpoint_class == LAB_CLASS for v in candidate.voters):
            return ModelReceiptVerifyOutcome(accepted=False, reason="no_lab_voter")
        return ModelReceiptVerifyOutcome(accepted=True, receipt_id=receipt_id)


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    order: tuple[str, ...] = ("lab", "harness"),
    world: World | None = None,
) -> Harness:
    return build_harness(
        "inmemory",
        monkeypatch,
        tmp_path,
        overlay=chain_overlay(order),
        world=world or World(),
    )


def _review_of(receipt: ModelAgentReceipt, head: str = HEAD) -> ModelReviewView:
    """The pull request review the dispatch posts for ``receipt``, as the gate reads it."""
    return ModelReviewView(
        author_login=PUBLISHER,
        commit_id=head,
        state="COMMENTED",
        body=render_receipt_review(receipt, FENCE),
    )


def _read_by_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reviews: list[ModelReviewView],
    verifier: SignatureVerifier,
    head: str = HEAD,
) -> Any:
    """Run the hosted gate over ``reviews`` for ``head``."""
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "agent_review_gate_accept",
        {
            "publisher_login": PUBLISHER,
            "verifier_endpoint": "http://verifier.test:8080",
            "transport": "in_memory",
        },
    )
    # The dispatch's overlay shares this root; the helper above rewrote only the gate's file.
    gate = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews(reviews),
        verifier=verifier,
        files=FakeFiles(),
    )
    return gate.handle_receipt(
        ModelReceiptGateRequest(
            repository=REPO,
            pull_request=PR,
            head_sha=head,
            step_output_path=OUTPUT_PATH,
            step_summary_path=SUMMARY_PATH,
        )
    )


async def test_golden_the_signed_receipt_carries_the_lab_model_voters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = _harness(monkeypatch, tmp_path)
    terminal = await h.run(h.request())

    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert terminal.reason is None
    receipt = h.world.stored[KEY]
    assert terminal.receipt_id == receipt.id
    assert receipt.signature
    assert receipt.head_sha == HEAD
    assert [
        (v.identity, v.endpoint_class, v.model, v.delegation_event_id)
        for v in receipt.voters
    ] == [
        ("lab-a", LAB_CLASS, "model-lab-a", "event-lab-a"),
        ("lab-b", LAB_CLASS, "model-lab-b", "event-lab-b"),
    ]
    # The chain asked the lab rung alone: the harness rung was never called.
    assert sorted(h.world.lab_calls) == ["lab-a", "lab-b"]
    assert [(a.rung, a.outcome) for a in terminal.attempts] == [
        ("lab", EnumAgentRungOutcome.ANSWERED),
        ("lab", EnumAgentRungOutcome.ANSWERED),
    ]
    assert h.world.posts == [receipt.id]


async def test_golden_the_gate_accepts_the_posted_receipt_for_the_exact_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = _harness(monkeypatch, tmp_path)
    await h.run(h.request())
    receipt = h.world.stored[KEY]
    verifier = SignatureVerifier()

    result = _read_by_gate(monkeypatch, tmp_path, [_review_of(receipt)], verifier)

    assert result.decision is EnumGateAcceptDecision.PASS_AGENT
    assert result.reason is EnumGateAcceptReason.RECEIPT_ACCEPTED
    assert result.receipt_id == receipt.id
    assert [c.head_sha for c in verifier.calls] == [HEAD]
    seen = verifier.calls[0].receipt
    assert [v["identity"] for v in seen["voters"]] == ["lab-a", "lab-b"]
    assert {v["endpoint_class"] for v in seen["voters"]} == {LAB_CLASS}


async def test_golden_a_lab_voter_still_answers_after_an_unreachable_first_rung(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = World(
        lab_raises={"harness-a"}, lab_error_text={"harness-a": RECORDED_CREDIT_503}
    )
    h = _harness(monkeypatch, tmp_path, order=("harness", "lab"), world=world)
    terminal = await h.run(h.request())

    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert world.lab_calls == ["harness-a", "lab-a", "lab-b"]
    receipt = world.stored[KEY]
    # The receipt carries the voters that answered, and the roster it was drawn from.
    assert [v.identity for v in receipt.voters] == ["lab-a", "lab-b"]
    assert receipt.roster_snapshot.voters == ("lab-a", "lab-b", "harness-a")
    result = _read_by_gate(
        monkeypatch, tmp_path, [_review_of(receipt)], SignatureVerifier()
    )
    assert result.decision is EnumGateAcceptDecision.PASS_AGENT


async def test_error_an_unreachable_rung_is_typed_on_the_terminal_and_not_in_the_votes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = World(
        lab_raises={"harness-a"}, lab_error_text={"harness-a": RECORDED_CREDIT_503}
    )
    h = _harness(monkeypatch, tmp_path, order=("harness", "lab"), world=world)
    terminal = await h.run(h.request())

    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    typed = [(a.rung, a.identity, a.outcome, a.cause) for a in terminal.attempts]
    assert typed == [
        ("harness", "harness-a", EnumAgentRungOutcome.UNAVAILABLE, "ConnectionError"),
        ("lab", "lab-a", EnumAgentRungOutcome.ANSWERED, ""),
        ("lab", "lab-b", EnumAgentRungOutcome.ANSWERED, ""),
    ]
    assert "harness-a" not in {v.identity for v in world.stored[KEY].voters}
    assert world.terminals["cmd-1"].attempts == terminal.attempts


async def test_error_a_timed_out_rung_is_typed_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = World(lab_slow={"harness-a"})
    h = _harness(monkeypatch, tmp_path, order=("harness", "lab"), world=world)
    terminal = await h.run(h.request())

    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    first = terminal.attempts[0]
    assert (first.rung, first.outcome, first.cause) == (
        "harness",
        EnumAgentRungOutcome.TIMEOUT,
        "TimeoutError",
    )


async def test_error_no_rung_able_to_answer_is_refused_with_every_attempt_typed_and_no_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = World(
        lab_raises={"lab-a", "lab-b", "harness-a"},
        lab_error_text={"lab-a": RECORDED_CREDIT_503},
    )
    h = _harness(monkeypatch, tmp_path, world=world)
    terminal = await h.run(h.request())

    assert terminal.outcome is EnumAgentReviewOutcome.REFUSED
    assert terminal.reason is EnumAgentReviewReason.LAB_RUNG_INSUFFICIENT
    assert [(a.rung, a.identity, a.outcome) for a in terminal.attempts] == [
        ("lab", "lab-a", EnumAgentRungOutcome.UNAVAILABLE),
        ("lab", "lab-b", EnumAgentRungOutcome.UNAVAILABLE),
        ("harness", "harness-a", EnumAgentRungOutcome.UNAVAILABLE),
    ]
    assert all(a.cause == "ConnectionError" for a in terminal.attempts)
    assert world.sign_calls == 0
    assert world.stored == {}
    assert world.posts == []
    # With no receipt posted the gate finds none and refuses on its wait: it does not invent a voter result.
    verifier = SignatureVerifier()
    result = _read_by_gate(monkeypatch, tmp_path, [], verifier)
    assert result.decision is EnumGateAcceptDecision.REFUSE
    assert result.reason is EnumGateAcceptReason.RECEIPT_TIMEOUT
    assert verifier.calls == []


async def test_control_a_receipt_with_its_lab_voter_dropped_after_signing_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = _harness(monkeypatch, tmp_path)
    await h.run(h.request())
    receipt = h.world.stored[KEY]
    tampered = receipt.model_copy(update={"voters": receipt.voters[:1]})

    result = _read_by_gate(
        monkeypatch, tmp_path, [_review_of(tampered)], SignatureVerifier()
    )

    assert result.decision is EnumGateAcceptDecision.REFUSE
    assert result.reason is EnumGateAcceptReason.RECEIPT_REFUSED
    assert "signature_invalid" in result.summary_line


async def test_control_a_receipt_read_for_another_head_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = _harness(monkeypatch, tmp_path)
    await h.run(h.request())
    receipt = h.world.stored[KEY]
    verifier = SignatureVerifier()

    result = _read_by_gate(
        monkeypatch, tmp_path, [_review_of(receipt)], verifier, head=MOVED
    )

    assert result.decision is EnumGateAcceptDecision.REFUSE
    assert result.reason is EnumGateAcceptReason.RECEIPT_TIMEOUT
    assert verifier.calls == []
