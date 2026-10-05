# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrHandoffDecision: ready, wait or refuse one PR handoff (OMN-20636).

Pure and deterministic: the verdict is a function of the request, the PR
watcher's newest observation of the PR, the live ledger holds and the
evaluating time, all handed in. It reads no clock, no file and no network.

The rules are pr-handoff's handoff_row.sh readiness rules, in its order, with
two changes that are the point of this node:

* the PR comes from the watcher's observation the orchestrator holds, never
  from a copy of the watcher's state file on the requesting host, so a PR the
  lane just opened is waited for instead of refused (exit 5 before);
* a head the watcher has not caught up with yet is a wait, not a refusal: an
  observation made before the request cannot know the lane's push. Only an
  observation made after the request that shows another head is stale_head.

The rows are the ones handoff_row.sh prints, stamped with the evaluating time,
so the landing controller reads the handoff exactly as before.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal

from omnimarket.events.pr_state import EnumPrState, ModelPrStateEmitRequest
from omnimarket.merge_control.hold_marker import evaluate_merge_hold
from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    EnumPrHandoffLabProofSource,
    EnumPrHandoffMode,
    EnumPrHandoffNeeds,
    EnumPrHandoffVerdict,
    EnumPrHandoffWaitReason,
    ModelPrHandoffDecision,
    ModelPrHandoffDecisionRequest,
    ModelPrHandoffRequested,
)

_OMN = re.compile(r"\bOMN-\d+\b")
_OMN_ANY_CASE = re.compile(r"\bomn-\d+\b", re.IGNORECASE)
_LAB_WORD = re.compile(r"(?im)\blab\b")
_LAB_LINE = re.compile(r"(?im)^\W*lab\b")
_HEAD_CELL = re.compile(r"\bhead=([0-9a-f]{7,40})(?![0-9A-Za-z])")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_WHAT: dict[EnumPrHandoffNeeds, str] = {
    EnumPrHandoffNeeds.LAND: "ready to land at this head",
    EnumPrHandoffNeeds.TRAIN: "runtime-affecting, ready for the runtime train at this head",
    EnumPrHandoffNeeds.COMPANION: "waiting on its change-control companion at this head",
}
# The PR watcher's full re-list interval (pr_state_local.py's freshness rule):
# an observation older than this before the request is not trusted.
OBSERVATION_FRESHNESS = timedelta(minutes=90)


def is_bot_release_pr(title: str, author_is_bot: bool) -> bool:
    """A bot's ``chore: release`` PR: the release train's, never handed off (OMN-20342).

    The same predicate as merge-drain's landing_facts.is_bot_release_pr.
    """
    return bool(author_is_bot) and title.lower().startswith("chore: release")


def invalid_request_reason(request: ModelPrHandoffRequested) -> str | None:
    """Why a request can never be handed off as written, or None.

    These are pr-handoff's usage refusals (exit 64), checked before the
    request is accepted so the lane hears about them at once.
    """
    if request.to_lane == request.lane:
        return (
            f"{request.lane} is the landing lane; a lane does not hand a PR to itself"
        )
    if request.mode is EnumPrHandoffMode.SESSION and request.msg_only:
        return "session mode and msg_only do not combine: the session STATUS is what releases the PR"
    closes_with_terminal = (
        request.mode is EnumPrHandoffMode.LANE and not request.msg_only
    )
    if closes_with_terminal and not request.delegation:
        return (
            "a TERMINAL close needs a delegation cell (OMN-17427): 'delegated=<n> runs=<ids>', "
            "'delegated=0 delegation_reason=<why>' or 'delegation=na:<reason>'"
        )
    return None


def _observed_at(observation: ModelPrStateEmitRequest) -> datetime:
    return datetime.fromisoformat(observation.observed_at.replace("Z", "+00:00"))


def _wait(
    reason: EnumPrHandoffWaitReason, detail: str, head: str | None
) -> ModelPrHandoffDecision:
    return ModelPrHandoffDecision(
        verdict=EnumPrHandoffVerdict.WAIT,
        wait_reason=reason,
        detail=detail,
        live_head_sha=head,
    )


def _refuse(
    code: EnumPrHandoffErrorCode, detail: str, head: str | None
) -> ModelPrHandoffDecision:
    return ModelPrHandoffDecision(
        verdict=EnumPrHandoffVerdict.REFUSE,
        error_code=code,
        detail=detail,
        live_head_sha=head,
    )


def _resolve_ticket(
    request: ModelPrHandoffRequested, observation: ModelPrStateEmitRequest
) -> tuple[str, Literal["title", "ticket-flag", "branch"]] | str:
    """The ticket and where it came from, or the refusal detail."""
    in_title: list[str] = sorted(set(_OMN.findall(observation.title)))
    source: Literal["title", "ticket-flag", "branch"]
    if len(in_title) > 1:
        return f"title cites {len(in_title)} ticket ids ({in_title}); a PR title cites exactly one"
    if len(in_title) == 1:
        ticket, source = in_title[0], "title"
    elif request.ticket is not None:
        ticket, source = request.ticket, "ticket-flag"
    else:
        in_branch = sorted(
            {m.upper() for m in _OMN_ANY_CASE.findall(observation.head_ref)}
        )
        if len(in_branch) != 1:
            return (
                f"title cites 0 ticket ids and the head branch {observation.head_ref!r} "
                f"names {len(in_branch)} ({in_branch}); pass a ticket"
            )
        ticket, source = in_branch[0], "branch"
    body = set(request.body_ticket_ids)
    if source == "title" and ticket not in body:
        cited = sorted(body)
        return f"body cites {cited}; it must include the title ticket {ticket}"
    if source != "title" and body and ticket not in body:
        return f"body cites {sorted(body)}; it must include the resolved ticket {ticket} or cite none"
    return ticket, source


def _lab_proof_problem(
    request: ModelPrHandoffRequested, observation: ModelPrStateEmitRequest
) -> str | None:
    proof = request.lab_proof
    head = observation.head_sha
    if proof is None:
        return (
            f'no lab proof: post a PR comment with a line "Lab: head={head} host=... lane=... '
            'command=... observed=..." and request the handoff again with it'
        )
    if proof.source is EnumPrHandoffLabProofSource.BODY:
        if _LAB_WORD.search(proof.line):
            return None
        return "the cited body line is not a lab line"
    if not _LAB_LINE.search(proof.line):
        return "the cited comment line does not start with Lab"
    shas = _HEAD_CELL.findall(proof.line)
    if any(head.startswith(sha) for sha in shas):
        return None
    named = shas[0][:7] if shas else "no head"
    return (
        f"the lab comment names {named} and the live head is {head[:7]}: "
        "post a new lab comment that names the live head"
    )


def _rows(
    request: ModelPrHandoffRequested,
    observation: ModelPrStateEmitRequest,
    ticket: str,
    source: str,
    now: datetime,
) -> tuple[str, str]:
    # Stamped with the request's time: the lane stopped touching the PR when it
    # asked, and one lane's requests are seconds apart, so the MSG id (stamp and
    # lane, unique per sender) never collides the way one watcher tick shared by
    # several PRs would.
    stamp = request.requested_at.strftime(_STAMP)
    lane, to, repo, head = (
        request.lane,
        request.to_lane,
        request.repo,
        observation.head_sha,
    )
    ref = f"{repo}#{request.pr_number}"
    msg_id = f"{stamp}-{lane}"
    provenance = (
        f" Decided at {now.strftime(_STAMP)} on the PR watcher's observation of "
        f"{observation.observed_at} by node_pr_handoff_orchestrator (OMN-20636)."
    )
    rows = [
        f"{stamp} | MSG | from={lane} | to={to} | id={msg_id} | ticket={ticket} | "
        f"source={source} | repo={repo} | pr={ref} | head={head} | needs={request.needs.value} | "
        f"Handoff from {lane}: {ref} is {_WHAT[request.needs]}. "
        f"Send a red back to {lane} by MSG.{provenance}"
    ]
    if request.mode is EnumPrHandoffMode.SESSION:
        rows.append(
            f"{stamp} | STATUS | lane={lane} | ticket={ticket} | handed-off={ref} | repo={repo} | "
            f"head={head} | handed_to={to} | msg={msg_id} | Session lane {lane} handed {ref} to {to} "
            "by MSG and stopped touching it; the session CLAIM stays open."
        )
    elif not request.msg_only:
        worktree = (
            f" | worktree={request.worktree_cell}" if request.worktree_cell else ""
        )
        rows.append(
            f"{stamp} | TERMINAL | lane={lane} | ticket={ticket} | outcome=handed-off | repo={repo} | "
            f"pr={ref} | head={head} | handed_to={to} | msg={msg_id} | friction={request.friction} | "
            f"{request.delegation}{worktree} | Handed {ref} to {to} by MSG; this lane has stopped "
            "touching the PR."
        )
    return msg_id, "\n".join(rows) + "\n"


def decide_handoff(
    decision_request: ModelPrHandoffDecisionRequest,
) -> ModelPrHandoffDecision:
    """The verdict for one evaluation of one handoff request."""
    request = decision_request.request
    observation = decision_request.observation
    if observation is None:
        return _wait(
            EnumPrHandoffWaitReason.PR_NOT_OBSERVED,
            f"the PR watcher has not observed {request.handoff_key} yet",
            None,
        )
    if (observation.repo, observation.pr_number) != (request.repo, request.pr_number):
        msg = f"observation {observation.repo}#{observation.pr_number} is not {request.handoff_key}"
        raise ValueError(msg)
    head = observation.head_sha
    if observation.state is not EnumPrState.OPEN:
        return _refuse(
            EnumPrHandoffErrorCode.PR_NOT_OPEN,
            f"state={observation.state.value}: only an open PR is handed off",
            head,
        )
    if is_bot_release_pr(observation.title, observation.author_is_bot):
        return _refuse(
            EnumPrHandoffErrorCode.WITHHELD,
            "a bot's `chore: release` PR is gated to the release train and landed through "
            "/omni:release-cut; it is never handed off",
            head,
        )
    if _observed_at(observation) < request.requested_at - OBSERVATION_FRESHNESS:
        return _wait(
            EnumPrHandoffWaitReason.OBSERVATION_STALE,
            f"the newest observation ({observation.observed_at}) is more than "
            f"{int(OBSERVATION_FRESHNESS.total_seconds() // 60)} minutes older than the request; "
            "waiting for the watcher to observe the PR again",
            head,
        )
    if not head.startswith(request.expected_head_sha):
        if _observed_at(observation) < request.requested_at:
            return _wait(
                EnumPrHandoffWaitReason.HEAD_NOT_OBSERVED,
                f"the newest observation ({observation.observed_at}) predates the request and "
                f"shows {head[:7]}, not {request.expected_head_sha[:7]}",
                head,
            )
        return _refuse(
            EnumPrHandoffErrorCode.STALE_HEAD,
            f"observed at {observation.observed_at}, after the request, the head is {head[:7]}, "
            f"not {request.expected_head_sha[:7]}: push again or read who pushed",
            head,
        )
    if observation.draft and request.needs is not EnumPrHandoffNeeds.COMPANION:
        return _wait(
            EnumPrHandoffWaitReason.DRAFT,
            "the PR is a draft: flip it ready, or request with needs=companion",
            head,
        )
    hold = evaluate_merge_hold(title=observation.title, labels=observation.labels)
    if not hold.is_merge_eligible:
        return _refuse(EnumPrHandoffErrorCode.HELD, hold.reason, head)
    if decision_request.ledger_holds:
        return _refuse(
            EnumPrHandoffErrorCode.HELD,
            f"live ledger HOLD rows name the PR or the lane: {', '.join(decision_request.ledger_holds)}",
            head,
        )
    resolved = _resolve_ticket(request, observation)
    if isinstance(resolved, str):
        return _refuse(EnumPrHandoffErrorCode.MISSING_TICKET, resolved, head)
    ticket, source = resolved
    if (
        request.handoff_key not in request.claim_prs
        and ticket not in request.claim_tickets
    ):
        return _refuse(
            EnumPrHandoffErrorCode.NOT_OWNED,
            f"no CLAIM of {request.lane} names {request.handoff_key} or its ticket {ticket}",
            head,
        )
    lab_problem = _lab_proof_problem(request, observation)
    if lab_problem is not None:
        return _refuse(EnumPrHandoffErrorCode.MISSING_LAB_PROOF, lab_problem, head)
    msg_id, rows = _rows(request, observation, ticket, source, decision_request.now)
    return ModelPrHandoffDecision(
        verdict=EnumPrHandoffVerdict.READY,
        detail=f"{request.handoff_key} at {head[:7]} is ready to hand to {request.to_lane}",
        live_head_sha=head,
        ticket=ticket,
        ticket_source=source,
        msg_id=msg_id,
        rows=rows,
    )


class HandlerPrHandoffDecision:
    """Definition-B compute: ``handle(ModelPrHandoffDecisionRequest) -> ModelPrHandoffDecision``."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(self, request: ModelPrHandoffDecisionRequest) -> ModelPrHandoffDecision:
        return decide_handoff(request)


__all__: list[str] = [
    "HandlerPrHandoffDecision",
    "decide_handoff",
    "invalid_request_reason",
    "is_bot_release_pr",
]
