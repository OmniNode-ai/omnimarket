# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the PR handoff workflow (OMN-20636).

Defined before the workflow was built (operator ruling 2026-10-05 ~18:45Z:
golden-chain and error-chain events validate the effect and compute
operations, and contracts determine how the nodes communicate). Every walker
obligation of node_pr_handoff_orchestrator (walker_report.json beside this
file) is bound to exactly one case here by ``chain_obligation``; the binding is
held by test_obligation_binding.py.

Nodes exercised, each by its real handler: node_pr_handoff_orchestrator,
node_pr_handoff_decision_compute (in process) and node_pr_handoff_ledger_effect.
Only the ledger host is scripted (tests/chains/pr_handoff/_builders.py).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    EnumPrHandoffLedgerStatus,
    EnumPrHandoffState,
    ModelPrHandoffFailed,
    ModelPrHandoffHandedOff,
    ModelPrHandoffLedgerAppended,
)
from omnimarket.nodes.node_work_ledger_append_effect.models import (
    EnumWorkLedgerAppendStatus,
)
from tests.chains.chain_assert import (
    assert_chain,
    assert_error_chain,
    chain_obligation,
    only,
)
from tests.chains.pr_handoff import _builders as b

pytestmark = pytest.mark.unit

ACCEPTED = EnumWorkLedgerAppendStatus.ACCEPTED
E = EnumPrHandoffErrorCode
S = EnumPrHandoffState

REQUESTED = "ModelPrHandoffRequested"
ACCEPTED_EVT = "ModelPrHandoffAccepted"
OBSERVED = "ModelPrHandoffObservationIngress"
APPEND = "ModelPrHandoffLedgerAppendCommand"
APPENDED = "ModelPrHandoffLedgerAppended"
HANDED_OFF = "ModelPrHandoffHandedOff"
FAILED = "ModelPrHandoffFailed"


# --------------------------------------------------------------------- golden


@chain_obligation("golden:accepted>evaluated_ready>append_accepted")
async def test_handoff_waits_for_first_observation_then_hands_off() -> None:
    """AC3: the lane's own new PR is not in any watcher state yet; it waits, then lands."""
    cid = b.new_cid()
    run = await b.drive(
        [ACCEPTED],
        [
            (b.request(cid), cid),
            (b.observation(b.at(120)), cid),
        ],
    )
    assert_chain(
        run.events,
        expected_event_types=[
            REQUESTED,
            ACCEPTED_EVT,
            OBSERVED,
            APPEND,
            APPENDED,
            HANDED_OFF,
        ],
        terminal_fields={
            "terminal_outcome": "handed_off",
            "handoff_key": b.KEY,
            "head_sha": b.HEAD,
            "ticket": b.TICKET,
            "to_lane": "landing-controller",
            "ledger_lines": (47200, 47201),
        },
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )
    # The rows are pr-handoff's, stamped for the ledger's bus append.
    (sent,) = run.ledger.requests
    msg, terminal = sent.rows.splitlines()
    stamp = b.stamp(b.at(120))
    assert msg.startswith(
        f"{stamp} | MSG | from={b.LANE} | to=landing-controller | id={stamp}-{b.LANE} | "
    )
    assert f"| pr={b.KEY} | head={b.HEAD} | needs=land |" in msg
    assert f"| req={sent.request_id} | via=bus:h202 |" in msg
    assert terminal.startswith(
        f"{stamp} | TERMINAL | lane={b.LANE} | ticket={b.TICKET} | outcome=handed-off | "
    )
    assert f"| req={sent.request_id} | via=bus:h202 |" in terminal
    assert "| delegated=0 delegation_reason=no-text-or-code |" in terminal
    handed = only(run.events, HANDED_OFF).payload
    assert isinstance(handed, ModelPrHandoffHandedOff)
    assert handed.ledger_request_id == sent.request_id


async def test_handoff_of_an_observed_pr_is_immediate() -> None:
    """A PR the watcher already observed at the pushed head is handed off in the request's leg."""
    cid = b.new_cid()
    run = await b.drive(
        [ACCEPTED],
        [
            (b.observation(b.at(-30)), cid),
            (b.request(cid), cid),
        ],
    )
    assert_chain(
        run.events,
        expected_event_types=[
            OBSERVED,
            REQUESTED,
            ACCEPTED_EVT,
            APPEND,
            APPENDED,
            HANDED_OFF,
        ],
        terminal_fields={"terminal_outcome": "handed_off", "head_sha": b.HEAD},
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )


async def test_head_not_yet_observed_waits_then_hands_off() -> None:
    """An observation older than the request at the old head is a wait, not stale_head."""
    cid = b.new_cid()
    run = await b.drive(
        [ACCEPTED],
        [
            (b.observation(b.at(-300), head_sha=b.OTHER_HEAD), cid),
            (b.request(cid), cid),
            (b.observation(b.at(90)), cid),
        ],
    )
    assert_chain(
        run.events,
        expected_event_types=[
            OBSERVED,
            REQUESTED,
            ACCEPTED_EVT,
            OBSERVED,
            APPEND,
            APPENDED,
            HANDED_OFF,
        ],
        terminal_fields={"terminal_outcome": "handed_off", "head_sha": b.HEAD},
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )


async def test_lost_receipt_is_resent_under_the_same_request_and_handed_off_once() -> (
    None
):
    """Mechanical retry: no receipt, then a duplicate receipt, ends handed off with one request id."""
    cid = b.new_cid()
    run = await b.drive(
        [None, EnumWorkLedgerAppendStatus.DUPLICATE],
        [(b.request(cid), cid), (b.observation(b.at(60)), cid)],
    )
    assert_chain(
        run.events,
        expected_event_types=[
            REQUESTED,
            ACCEPTED_EVT,
            OBSERVED,
            APPEND,
            APPENDED,
            APPEND,
            APPENDED,
            HANDED_OFF,
        ],
        terminal_fields={"terminal_outcome": "handed_off", "ledger_lines": ()},
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )
    first, second = run.ledger.requests
    assert first.request_id == second.request_id
    assert first.rows == second.rows
    statuses = [
        e.payload.status
        for e in run.events
        if isinstance(e.payload, ModelPrHandoffLedgerAppended)
    ]
    assert statuses == [
        EnumPrHandoffLedgerStatus.PENDING,
        EnumPrHandoffLedgerStatus.DUPLICATE,
    ]


# ---------------------------------------------------------------------- error


async def _refused_on_observation(
    **observation_overrides: object,
) -> tuple[UUID, b.HandoffRun]:
    cid = b.new_cid()
    run = await b.drive(
        [],
        [
            (b.request(cid), cid),
            (b.observation(b.at(60), **observation_overrides), cid),
        ],
    )
    return cid, run


async def _refused_by_request(
    **request_overrides: object,
) -> tuple[UUID, b.HandoffRun]:
    cid = b.new_cid()
    run = await b.drive(
        [],
        [(b.request(cid, **request_overrides), cid), (b.observation(b.at(60)), cid)],
    )
    return cid, run


async def _assert_refused(
    cid: UUID, run: b.HandoffRun, code: EnumPrHandoffErrorCode
) -> None:
    assert_error_chain(
        run.events,
        expected_event_types=[REQUESTED, ACCEPTED_EVT, OBSERVED, FAILED],
        terminal_fields={
            "error_code": code,
            "terminal_state": S.REFUSED,
            "handoff_key": b.KEY,
            "ledger_request_id": None,
        },
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )
    assert run.ledger.requests == []


@chain_obligation("error:rejected_invalid_request")
async def test_lane_handing_to_itself_is_refused_before_acceptance() -> None:
    cid = b.new_cid()
    run = await b.drive([], [(b.request(cid, to_lane=b.LANE), cid)])
    assert_error_chain(
        run.events,
        expected_event_types=[REQUESTED, FAILED],
        terminal_fields={"error_code": E.INVALID_REQUEST, "terminal_state": S.REFUSED},
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )


@chain_obligation("error:accepted>evaluated_stale_head")
async def test_head_observed_after_the_request_at_another_sha_is_stale() -> None:
    cid, run = await _refused_on_observation(head_sha=b.OTHER_HEAD)
    await _assert_refused(cid, run, E.STALE_HEAD)
    failed = only(run.events, FAILED).payload
    assert isinstance(failed, ModelPrHandoffFailed)
    assert failed.live_head_sha == b.OTHER_HEAD


@chain_obligation("error:accepted>evaluated_missing_ticket")
async def test_no_resolvable_ticket_is_refused() -> None:
    cid, run = await _refused_on_observation(
        title="feat: PR handoff over the bus", head_ref="jonah/pr-handoff-bus"
    )
    await _assert_refused(cid, run, E.MISSING_TICKET)


@chain_obligation("error:accepted>evaluated_not_owned")
async def test_pr_no_claim_of_the_lane_names_is_refused() -> None:
    cid, run = await _refused_by_request(claim_tickets=("OMN-1",), claim_prs=())
    await _assert_refused(cid, run, E.NOT_OWNED)


@chain_obligation("error:accepted>evaluated_missing_lab_proof")
async def test_missing_lab_comment_is_refused() -> None:
    cid, run = await _refused_by_request(lab_proof=None)
    await _assert_refused(cid, run, E.MISSING_LAB_PROOF)


@chain_obligation("error:accepted>evaluated_held")
async def test_hold_marker_on_the_live_pr_is_refused() -> None:
    cid, run = await _refused_on_observation(labels=["do-not-merge"])
    await _assert_refused(cid, run, E.HELD)


@chain_obligation("error:accepted>evaluated_withheld")
async def test_bot_release_train_pr_is_withheld() -> None:
    cid, run = await _refused_on_observation(
        author="omninode-release[bot]",
        author_is_bot=True,
        title="chore: release omnimarket 0.9.1",
    )
    await _assert_refused(cid, run, E.WITHHELD)


@chain_obligation("error:accepted>evaluated_pr_not_open")
async def test_closed_pr_is_refused() -> None:
    cid, run = await _refused_on_observation(state="closed")
    await _assert_refused(cid, run, E.PR_NOT_OPEN)


@chain_obligation("error:accepted>superseded")
async def test_newer_request_for_the_same_pr_supersedes_a_waiting_one() -> None:
    first, second = b.new_cid(), b.new_cid()
    run = await b.drive(
        [],
        [
            (b.request(first), first),
            (b.request(second, requested_at=b.at(30)), second),
        ],
    )
    assert_error_chain(
        run.events_for(first),
        expected_event_types=[REQUESTED, ACCEPTED_EVT, FAILED],
        terminal_fields={"error_code": E.SUPERSEDED, "terminal_state": S.REFUSED},
        correlation_id=first,
    )
    assert [e.event_type for e in run.events_for(second)] == [REQUESTED, ACCEPTED_EVT]


@chain_obligation("error:accepted>completion_bound_expired")
async def test_wait_past_the_budget_times_out() -> None:
    cid = b.new_cid()
    run = await b.drive(
        [],
        [
            (b.request(cid, wait_budget_s=60), cid),
            (b.observation(b.at(61), draft=True), cid),
        ],
    )
    assert_error_chain(
        run.events,
        expected_event_types=[REQUESTED, ACCEPTED_EVT, OBSERVED, FAILED],
        terminal_fields={"error_code": E.TIMED_OUT, "terminal_state": S.TIMED_OUT},
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )


@chain_obligation("error:accepted>evaluated_ready>append_refused")
async def test_ledger_refusal_fails_the_handoff() -> None:
    cid = b.new_cid()
    run = await b.drive(
        [EnumWorkLedgerAppendStatus.REFUSED],
        [(b.request(cid), cid), (b.observation(b.at(60)), cid)],
    )
    assert_error_chain(
        run.events,
        expected_event_types=[
            REQUESTED,
            ACCEPTED_EVT,
            OBSERVED,
            APPEND,
            APPENDED,
            FAILED,
        ],
        terminal_fields={"error_code": E.LEDGER_REFUSED, "terminal_state": S.REFUSED},
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )


@chain_obligation("error:accepted>evaluated_ready>append_attempts_exhausted")
async def test_every_append_unanswered_times_out_unconfirmed() -> None:
    cid = b.new_cid()
    run = await b.drive(
        [None, None, None],
        [(b.request(cid), cid), (b.observation(b.at(60)), cid)],
    )
    assert_error_chain(
        run.events,
        expected_event_types=[
            REQUESTED,
            ACCEPTED_EVT,
            OBSERVED,
            APPEND,
            APPENDED,
            APPEND,
            APPENDED,
            APPEND,
            APPENDED,
            FAILED,
        ],
        terminal_fields={
            "error_code": E.APPEND_UNCONFIRMED,
            "terminal_state": S.TIMED_OUT,
        },
        correlation_id=cid,
        bus_history_count=await run.bus_history_count(),
    )
    assert len({r.request_id for r in run.ledger.requests}) == 1
