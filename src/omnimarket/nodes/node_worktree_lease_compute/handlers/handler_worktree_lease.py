# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a second lane starting in a worktree a live ledger CLAIM holds."""

import re
from pathlib import Path

from omnimarket.handlers.handler_ledger_claims import (
    claim_lane,
    claim_names_tree,
    claim_stamp,
    live_claims,
)
from omnimarket.nodes.node_worktree_lease_compute.models.model_worktree_lease import (
    EnumWorktreeLeaseReason as Reason,
)
from omnimarket.nodes.node_worktree_lease_compute.models.model_worktree_lease import (
    EnumWorktreeLeaseVerdict as Verdict,
)
from omnimarket.nodes.node_worktree_lease_compute.models.model_worktree_lease import (
    ModelWorktreeLeaseDecision,
    ModelWorktreeLeaseRequest,
)

_DATED_ROW = re.compile(r"^\s*\|?\s*\d{4}-\d{2}-\d{2}T", re.MULTILINE)
_TICKET = re.compile(r"(?:^|[\s|])ticket=([^\s|,]+)")


def release_path(
    holder_lane: str, claim_row: str, ticket: str, stale_hours: float
) -> str:
    """The rows that free the tree: the holder releases, then the new lane claims."""
    return (
        "Release path -- the holder releases, then the new lane claims:\n"
        f"    <ts> | RELEASE | lane={holder_lane} | re={claim_stamp(claim_row)} | ticket={ticket} | <why it is being given up>\n"
        f"    <ts> | CLAIM | lane=<your lane> | ticket={ticket} | <scope and cost sentence>\n"
        f"and if that lane is gone, wait for the claim to go stale "
        f"({stale_hours:g}h with no row from it) and then claim the ticket naming the stale claim:\n"
        f"    <ts> | CLAIM | lane=<your lane> | ticket={ticket} | "
        f"supersedes-claim={claim_stamp(claim_row)} | <why>"
    )


class HandlerWorktreeLeaseCompute:
    def handle(self, request: ModelWorktreeLeaseRequest) -> ModelWorktreeLeaseDecision:
        common = {
            "requester_lane": request.requester_lane,
            "worktree_path": request.worktree_path,
        }
        # A ledger with no dated row reads as "nobody holds anything"; that zero
        # must not grant a lease, so an unreadable ledger refuses.
        if not _DATED_ROW.search(request.ledger_text):
            return ModelWorktreeLeaseDecision(
                verdict=Verdict.REFUSED,
                reason=Reason.LEDGER_UNREADABLE,
                refusal=(
                    f"worktree {request.worktree_path} refused for lane "
                    f"{request.requester_lane}: the ledger carries no dated row, so "
                    "no holder can be ruled out"
                ),
                **common,
            )
        path = Path(request.worktree_path)
        root = Path(request.root)
        on_tree = [
            row
            for row in live_claims(
                request.ledger_text, request.now, request.stale_after_hours
            )
            if claim_names_tree(path, root, row)
        ]
        holders: dict[str, str] = {}
        for row in on_tree:
            lane = claim_lane(row)
            if lane is not None and lane != request.requester_lane:
                holders[lane] = row
        if not holders:
            return ModelWorktreeLeaseDecision(
                verdict=Verdict.GRANTED,
                reason=Reason.HELD_BY_REQUESTER if on_tree else Reason.UNCLAIMED,
                **common,
            )
        holder_lane, holder_row = next(iter(holders.items()))
        ticket_match = _TICKET.search(holder_row)
        ticket = (
            ticket_match[1]
            if ticket_match
            else (path.name if path.parent == root else path.parent.name)
        )
        path_text = release_path(
            holder_lane, holder_row, ticket, request.stale_after_hours
        )
        return ModelWorktreeLeaseDecision(
            verdict=Verdict.REFUSED,
            reason=Reason.HELD_BY_OTHER_LANE,
            holder_lane=holder_lane,
            holder_claim_row=holder_row,
            other_holder_lanes=tuple(holders)[1:],
            release_path=path_text,
            refusal=(
                f"worktree {request.worktree_path} refused for lane "
                f"{request.requester_lane}: held by lane {holder_lane} "
                f"(claim {claim_stamp(holder_row)}).\n{path_text}"
            ),
            **common,
        )
