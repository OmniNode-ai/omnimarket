# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_handoff_decision_compute: every rule, and the rows pr-handoff wrote (OMN-20636)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from omnimarket.events.pr_state import ModelPrStateEmitRequest
from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    EnumPrHandoffLabProofSource,
    EnumPrHandoffMode,
    EnumPrHandoffNeeds,
    EnumPrHandoffVerdict,
    EnumPrHandoffWaitReason,
    ModelPrHandoffDecision,
    ModelPrHandoffDecisionRequest,
    ModelPrHandoffLabProof,
)
from omnimarket.nodes.node_pr_handoff_decision_compute.handlers.handler_pr_handoff_decision import (
    HandlerPrHandoffDecision,
    invalid_request_reason,
    is_bot_release_pr,
)
from tests.chains.pr_handoff import _builders as b

pytestmark = pytest.mark.unit

CID = UUID("6f0c3a52-8d2b-4a51-9a50-55a1d3f2b0c4")
E = EnumPrHandoffErrorCode
W = EnumPrHandoffWaitReason


def _decide(
    observed_offset: int | None = 60,
    *,
    holds: tuple[str, ...] = (),
    now_offset: int = 60,
    request: dict[str, Any] | None = None,
    observation: dict[str, Any] | None = None,
) -> ModelPrHandoffDecision:
    obs: ModelPrStateEmitRequest | None = None
    if observed_offset is not None:
        obs = b.observation(b.at(observed_offset), **(observation or {})).observation()
    return HandlerPrHandoffDecision().handle(
        ModelPrHandoffDecisionRequest(
            request=b.request(CID, **(request or {})),
            observation=obs,
            ledger_holds=holds,
            now=b.at(now_offset),
        )
    )


def _refused(decision: ModelPrHandoffDecision, code: EnumPrHandoffErrorCode) -> None:
    assert decision.verdict is EnumPrHandoffVerdict.REFUSE
    assert decision.error_code is code
    assert decision.rows is None


def _waits(decision: ModelPrHandoffDecision, reason: EnumPrHandoffWaitReason) -> None:
    assert decision.verdict is EnumPrHandoffVerdict.WAIT
    assert decision.wait_reason is reason


def test_ready_rows_are_handoff_row_sh_rows_stamped_at_now() -> None:
    decision = _decide()
    assert decision.verdict is EnumPrHandoffVerdict.READY
    stamp = b.stamp(b.at(60))
    ref = b.KEY
    assert decision.msg_id == f"{stamp}-{b.LANE}"
    assert decision.rows == (
        f"{stamp} | MSG | from={b.LANE} | to=landing-controller | id={stamp}-{b.LANE} | "
        f"ticket={b.TICKET} | source=title | repo={b.REPO} | pr={ref} | head={b.HEAD} | "
        f"needs=land | Handoff from {b.LANE}: {ref} is ready to land at this head. "
        f"Send a red back to {b.LANE} by MSG. Decided on the PR watcher's observation by "
        "node_pr_handoff_orchestrator (OMN-20636).\n"
        f"{stamp} | TERMINAL | lane={b.LANE} | ticket={b.TICKET} | outcome=handed-off | "
        f"repo={b.REPO} | pr={ref} | head={b.HEAD} | handed_to=landing-controller | "
        f"msg={stamp}-{b.LANE} | friction=none | delegated=0 delegation_reason=no-text-or-code"
        f" | Handed {ref} to landing-controller by MSG; this lane has stopped touching the PR.\n"
    )


def test_session_mode_closes_with_status_and_msg_only_with_nothing() -> None:
    session = _decide(request={"mode": EnumPrHandoffMode.SESSION, "delegation": None})
    assert session.rows is not None
    msg, status = session.rows.splitlines()
    assert " | MSG | from=" in msg
    assert " | STATUS | lane=" in status
    assert f"handed-off={b.KEY}" in status
    only_msg = _decide(request={"msg_only": True, "delegation": None})
    assert only_msg.rows is not None
    assert len(only_msg.rows.splitlines()) == 1


def test_worktree_cell_precedes_the_free_text_of_the_terminal() -> None:
    decision = _decide(request={"worktree_cell": "removed:omni_worktrees/x/omnimarket"})
    assert decision.rows is not None
    assert "| worktree=removed:omni_worktrees/x/omnimarket | Handed " in decision.rows


@pytest.mark.parametrize(
    ("needs", "what"),
    [
        (EnumPrHandoffNeeds.TRAIN, "runtime-affecting, ready for the runtime train"),
        (EnumPrHandoffNeeds.COMPANION, "waiting on its change-control companion"),
    ],
)
def test_needs_names_the_ask(needs: EnumPrHandoffNeeds, what: str) -> None:
    decision = _decide(request={"needs": needs})
    assert decision.rows is not None
    assert f"needs={needs.value} |" in decision.rows
    assert what in decision.rows


def test_no_observation_waits() -> None:
    _waits(_decide(None), W.PR_NOT_OBSERVED)


def test_head_lag_before_the_request_waits_and_after_it_is_stale() -> None:
    _waits(_decide(-1, observation={"head_sha": b.OTHER_HEAD}), W.HEAD_NOT_OBSERVED)
    _refused(_decide(0, observation={"head_sha": b.OTHER_HEAD}), E.STALE_HEAD)


def test_draft_waits_unless_companion() -> None:
    _waits(_decide(observation={"draft": True}), W.DRAFT)
    ready = _decide(
        observation={"draft": True}, request={"needs": EnumPrHandoffNeeds.COMPANION}
    )
    assert ready.verdict is EnumPrHandoffVerdict.READY


def test_closed_and_merged_are_not_open() -> None:
    _refused(_decide(observation={"state": "merged"}), E.PR_NOT_OPEN)


def test_release_train_pr_is_withheld_only_for_a_bot() -> None:
    assert is_bot_release_pr("chore: release omnimarket 0.9.1", True)
    assert not is_bot_release_pr("chore: release omnimarket 0.9.1", False)
    _refused(
        _decide(observation={"author_is_bot": True, "title": "Chore: Release 1.0"}),
        E.WITHHELD,
    )


def test_hold_marker_and_ledger_hold_refuse() -> None:
    _refused(_decide(observation={"title": f"WIP feat({b.TICKET}): x"}), E.HELD)
    _refused(_decide(holds=("2026-10-05T18:00:00Z-orchestrator",)), E.HELD)


def test_ticket_from_flag_or_branch_when_the_title_has_none() -> None:
    no_title = {"title": "feat: public kb page", "head_ref": "jonah/omn-20636-x"}
    from_branch = _decide(observation=no_title, request={"body_ticket_ids": ()})
    assert from_branch.ticket == b.TICKET
    assert from_branch.ticket_source == "branch"
    from_flag = _decide(
        observation={**no_title, "head_ref": "kb-page"},
        request={"ticket": b.TICKET, "body_ticket_ids": ()},
    )
    assert from_flag.ticket_source == "ticket-flag"


def test_ticket_refusals() -> None:
    _refused(
        _decide(observation={"title": f"feat({b.TICKET}, OMN-1): two"}),
        E.MISSING_TICKET,
    )
    _refused(_decide(request={"body_ticket_ids": ()}), E.MISSING_TICKET)
    _refused(_decide(request={"body_ticket_ids": ("OMN-1",)}), E.MISSING_TICKET)


def test_ownership_by_pr_reference_or_ticket() -> None:
    by_pr = _decide(request={"claim_tickets": (), "claim_prs": (b.KEY,)})
    assert by_pr.verdict is EnumPrHandoffVerdict.READY
    _refused(_decide(request={"claim_tickets": ("OMN-2",)}), E.NOT_OWNED)


def test_lab_proof_rules() -> None:
    stale = b.lab_comment(head=b.OTHER_HEAD)
    _refused(_decide(request={"lab_proof": stale}), E.MISSING_LAB_PROOF)
    not_lab = ModelPrHandoffLabProof(
        source=EnumPrHandoffLabProofSource.COMMENT, line=f"ran it head={b.HEAD}"
    )
    _refused(_decide(request={"lab_proof": not_lab}), E.MISSING_LAB_PROOF)
    body = ModelPrHandoffLabProof(
        source=EnumPrHandoffLabProofSource.BODY, line="Lab: host=h202 observed=ok"
    )
    assert _decide(request={"lab_proof": body}).verdict is EnumPrHandoffVerdict.READY
    exempt = ModelPrHandoffLabProof(
        source=EnumPrHandoffLabProofSource.EXEMPT_VERSION_BUMP
    )
    _refused(_decide(request={"lab_proof": exempt}), E.MISSING_LAB_PROOF)
    bot = _decide(request={"lab_proof": exempt}, observation={"author_is_bot": True})
    assert bot.rows is not None
    assert "Lab line exempt: bot version-only bump" in bot.rows


def test_invalid_requests() -> None:
    assert invalid_request_reason(b.request(CID)) is None
    assert invalid_request_reason(b.request(CID, to_lane=b.LANE)) is not None
    assert invalid_request_reason(b.request(CID, delegation=None)) is not None
    session_msg_only = b.request(CID, mode=EnumPrHandoffMode.SESSION, msg_only=True)
    assert invalid_request_reason(session_msg_only) is not None


def test_the_decision_is_a_function_of_its_inputs() -> None:
    first, second = _decide(), _decide()
    assert first == second
    later = _decide(now_offset=60 + int(timedelta(minutes=1).total_seconds()))
    assert later.rows != first.rows


def test_an_observation_of_another_pr_is_a_programming_error() -> None:
    other = b.observation(b.at(60), pr_number=b.PR + 1).observation()
    with pytest.raises(ValueError, match="is not"):
        HandlerPrHandoffDecision().handle(
            ModelPrHandoffDecisionRequest(
                request=b.request(CID), observation=other, now=b.at(60)
            )
        )
