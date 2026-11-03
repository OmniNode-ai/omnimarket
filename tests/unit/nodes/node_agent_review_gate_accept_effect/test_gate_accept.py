# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate-accept node decides ``unchanged``, ``pass_agent`` or ``refuse``, and fails closed.

Each test runs in both profiles (see ``conftest``) with fake review and verifier ports.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptDecision as Decision,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptReason as Reason,
)

from .conftest import Gate, request_for
from .support import (
    CREDIT_ERROR,
    HEAD,
    OTHER_HEAD,
    OUTPUT_PATH,
    PR,
    PUBLISHER,
    REPO,
    SUMMARY_PATH,
    TIMEOUT_ERROR,
    UNAVAILABLE_ERROR,
    VERIFIER_ENDPOINT,
    FakeVerifier,
    accepted,
    degraded_with,
    plain_review,
    receipt_review,
    refused,
    result_file,
    voter,
)

MakeGate = Callable[..., Gate]
LOST_BOTH = degraded_with(CREDIT_ERROR, UNAVAILABLE_ERROR)


async def test_no_receipt_leaves_the_verdict_unchanged(make_gate: MakeGate) -> None:
    gate = make_gate(LOST_BOTH, [plain_review()])
    result = await gate.decide(request_for())
    assert result.decision is Decision.UNCHANGED
    assert result.reason is Reason.NO_RECEIPT_REVIEW
    assert result.receipt_id == ""
    assert gate.verifier.calls == []


async def test_a_valid_passing_receipt_with_degraded_voters_passes_agent(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(
        LOST_BOTH,
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.PASS_AGENT
    assert result.reason is Reason.RECEIPT_ACCEPTED
    assert result.receipt_id == "rcpt-1"
    assert "source=agent receipt=rcpt-1" in result.summary_line
    (call,) = gate.verifier.calls
    assert (call.repository, call.pull_request, call.head_sha) == (REPO, PR, HEAD)
    assert call.verifier_endpoint == VERIFIER_ENDPOINT
    assert call.receipt["id"] == "rcpt-1"


async def test_the_same_receipt_with_voters_that_passed_is_unchanged(
    make_gate: MakeGate,
) -> None:
    passed = result_file(voter("voter-1"), voter("voter-2"), verdict="passed")
    gate = make_gate(
        passed, [receipt_review("rcpt-1")], FakeVerifier({"rcpt-1": accepted("rcpt-1")})
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.UNCHANGED
    assert result.reason is Reason.VOTERS_PASSED
    assert gate.verifier.calls == []


async def test_a_blocking_finding_from_a_succeeded_voter_is_unchanged(
    make_gate: MakeGate,
) -> None:
    mixed = result_file(
        voter("voter-1", severities=("critical",)), voter("voter-2", error=CREDIT_ERROR)
    )
    gate = make_gate(
        mixed, [receipt_review("rcpt-1")], FakeVerifier({"rcpt-1": accepted("rcpt-1")})
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.UNCHANGED
    assert result.reason is Reason.BLOCKING_FINDING_FROM_SUCCEEDED_VOTER
    assert gate.verifier.calls == []


async def test_a_non_blocking_finding_from_a_succeeded_voter_does_not_stop_a_pass(
    make_gate: MakeGate,
) -> None:
    mixed = result_file(
        voter("voter-1", severities=("warning",)), voter("voter-2", error=CREDIT_ERROR)
    )
    gate = make_gate(
        mixed, [receipt_review("rcpt-1")], FakeVerifier({"rcpt-1": accepted("rcpt-1")})
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.PASS_AGENT


async def test_a_ci_blocked_verdict_is_never_overridden(make_gate: MakeGate) -> None:
    blocked = result_file(voter("voter-1"), voter("voter-2"), verdict="blocked")
    gate = make_gate(
        blocked,
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.UNCHANGED
    assert result.reason is Reason.CI_BLOCKED
    assert gate.verifier.calls == []


async def test_a_forged_receipt_in_a_review_by_the_publisher_is_refused(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(
        LOST_BOTH,
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": refused("signature_invalid")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason.RECEIPT_REFUSED
    assert "signature_invalid" in result.summary_line


async def test_a_receipt_for_another_head_in_a_review_by_the_publisher_is_refused(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(
        LOST_BOTH,
        [receipt_review("rcpt-old", commit_id=OTHER_HEAD)],
        FakeVerifier({"rcpt-old": refused("head_differs")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason.RECEIPT_REFUSED
    assert "head_differs" in result.summary_line
    (call,) = gate.verifier.calls
    assert call.head_sha == HEAD, (
        "the verifier is asked about the head the gate runs on"
    )


async def test_a_receipt_in_a_review_by_another_identity_is_ignored(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(
        LOST_BOTH,
        [receipt_review("rcpt-1", author="someone-else")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.UNCHANGED
    assert result.reason is Reason.NO_RECEIPT_REVIEW
    assert gate.verifier.calls == []


async def test_the_publisher_identity_is_matched_without_regard_to_case(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(
        LOST_BOTH,
        [receipt_review("rcpt-1", author=PUBLISHER.upper())],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    assert (await gate.decide(request_for())).decision is Decision.PASS_AGENT


@pytest.mark.parametrize(
    "content",
    [None, "", "not json", "[]", '{"results": "none"}', '{"quorum": {}}'],
    ids=["missing", "empty", "garbage", "not-an-object", "no-voters", "no-results"],
)
async def test_an_unreadable_voter_result_file_is_refused(
    make_gate: MakeGate, content: str | None
) -> None:
    gate = make_gate(
        content,
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason.VOTER_RESULT_UNREADABLE
    assert gate.verifier.calls == []


async def test_a_blocked_receipt_is_refused(make_gate: MakeGate) -> None:
    gate = make_gate(
        LOST_BOTH,
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": refused("verdict_blocked")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert "verdict_blocked" in result.summary_line


async def test_one_accepted_receipt_wins_over_a_refused_earlier_one(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(
        LOST_BOTH,
        [receipt_review("rcpt-old", commit_id=OTHER_HEAD), receipt_review("rcpt-new")],
        FakeVerifier(
            {"rcpt-old": refused("head_differs"), "rcpt-new": accepted("rcpt-new")}
        ),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.PASS_AGENT
    assert result.receipt_id == "rcpt-new"
    assert len(gate.verifier.calls) == 2


async def test_a_receipt_block_that_does_not_parse_is_refused(
    make_gate: MakeGate,
) -> None:
    broken = plain_review()
    broken = broken.model_copy(
        update={"body": "```onex-review-receipt\n{not json\n```"}
    )
    gate = make_gate(LOST_BOTH, [broken])
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason.RECEIPT_MALFORMED
    assert gate.verifier.calls == []


async def test_unreadable_reviews_refuse(make_gate: MakeGate) -> None:
    gate = make_gate(LOST_BOTH, reviews_fail=True)
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason.REVIEWS_UNREADABLE


async def test_an_unreachable_verifier_refuses(make_gate: MakeGate) -> None:
    gate = make_gate(LOST_BOTH, [receipt_review("rcpt-1")], FakeVerifier(fail=True))
    result = await gate.decide(request_for())
    assert result.decision is Decision.REFUSE
    assert result.reason is Reason.VERIFIER_UNREACHABLE


async def test_an_unexplained_voter_failure_is_unchanged(make_gate: MakeGate) -> None:
    unexplained = degraded_with(CREDIT_ERROR, "ValueError: the reply was empty")
    gate = make_gate(
        unexplained,
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.UNCHANGED
    assert result.reason is Reason.UNEXPLAINED_VOTER_FAILURE
    assert "voter-2=untyped" in result.summary_line
    assert gate.verifier.calls == []


async def test_a_degraded_result_with_no_unsuccessful_voter_is_unchanged(
    make_gate: MakeGate,
) -> None:
    odd = result_file(voter("voter-1"), voter("voter-2"), verdict="degraded_quorum")
    gate = make_gate(
        odd, [receipt_review("rcpt-1")], FakeVerifier({"rcpt-1": accepted("rcpt-1")})
    )
    result = await gate.decide(request_for())
    assert result.decision is Decision.UNCHANGED
    assert result.reason is Reason.NO_UNSUCCESSFUL_VOTER


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (CREDIT_ERROR, "credit_exhausted"),
        (UNAVAILABLE_ERROR, "unavailable"),
        (TIMEOUT_ERROR, "timeout"),
    ],
)
async def test_each_typed_cause_is_named_in_the_summary_line(
    make_gate: MakeGate, error: str, cause: str
) -> None:
    lost = result_file(voter("voter-1", error=error), voter("voter-2", error=error))
    for reviews, verifier in (
        ([], FakeVerifier()),
        ([receipt_review("rcpt-1")], FakeVerifier({"rcpt-1": accepted("rcpt-1")})),
        ([receipt_review("rcpt-1")], FakeVerifier({"rcpt-1": refused("head_differs")})),
    ):
        result = await make_gate(lost, reviews, verifier).decide(request_for())
        assert f"voter-1={cause}" in result.summary_line
        assert f"voter-2={cause}" in result.summary_line


async def test_a_different_cause_per_voter_is_named_per_voter(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(degraded_with(CREDIT_ERROR, TIMEOUT_ERROR))
    result = await gate.decide(request_for())
    assert "voter-1=credit_exhausted" in result.summary_line
    assert "voter-2=timeout" in result.summary_line


async def test_a_gateway_error_that_only_quotes_the_reason_is_not_credit_exhausted(
    make_gate: MakeGate,
) -> None:
    quoted = (
        "ValueError: the model wrote 'credit_exhausted' and"
        ' :: response_body={"error":{"type":"x","reason":"credit_exhausted"}}'
    )
    gate = make_gate(degraded_with(quoted, quoted))
    result = await gate.decide(request_for())
    assert result.reason is Reason.UNEXPLAINED_VOTER_FAILURE


async def test_the_step_output_and_summary_files_carry_the_decision(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(
        LOST_BOTH,
        [receipt_review("rcpt-1")],
        FakeVerifier({"rcpt-1": accepted("rcpt-1")}),
    )
    result = await gate.decide(request_for())
    output = gate.files.files[OUTPUT_PATH].splitlines()
    assert "decision=pass_agent" in output
    assert "reason=receipt_accepted" in output
    assert "receipt_id=rcpt-1" in output
    assert result.summary_line in gate.files.files[SUMMARY_PATH]


async def test_a_refusal_also_writes_its_reason(make_gate: MakeGate) -> None:
    gate = make_gate(None)
    await gate.decide(request_for())
    assert "decision=refuse" in gate.files.files[OUTPUT_PATH].splitlines()
    assert (
        "reason=voter_result_unreadable" in gate.files.files[OUTPUT_PATH].splitlines()
    )


async def test_the_reviews_are_read_for_the_requests_pull_request(
    make_gate: MakeGate,
) -> None:
    gate = make_gate(LOST_BOTH, [plain_review()])
    await gate.decide(request_for(pull_request=77))
    assert gate.reviews.reads == [(REPO, 77)]
