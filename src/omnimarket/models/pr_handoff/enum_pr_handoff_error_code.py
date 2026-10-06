# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a PR handoff ended without handing the PR off (OMN-20636).

Every value is one error chain of node_pr_handoff_orchestrator. The refusal
codes the decision compute returns map one to one onto the FSM trigger
``evaluated_<code>`` from WAITING to REFUSED, so each has its own walker path
and its own asserted chain.
"""

from __future__ import annotations

from enum import StrEnum


class EnumPrHandoffErrorCode(StrEnum):
    """The ``error_code`` of a failed handoff; one per error chain."""

    INVALID_REQUEST = "invalid_request"
    """The request cannot be handed off as written (a lane handing to itself, a TERMINAL close with no delegation cell)."""

    STALE_HEAD = "stale_head"
    """An observation made after the request shows a head other than the one the lane pushed: someone else pushed, or the push failed."""

    MISSING_TICKET = "missing_ticket"
    """No single OMN ticket resolves from the live title, the request's ticket or the head branch, or the body cites ids that exclude it."""

    NOT_OWNED = "not_owned"
    """No CLAIM of the requesting lane names the PR or carries its ticket."""

    MISSING_LAB_PROOF = "missing_lab_proof"
    """The request carries no lab proof that names the live head (and the PR is not an exempt bot version bump)."""

    HELD = "held"
    """A hold marker is on the live PR (title or label), or a live ledger HOLD names the PR or the lane."""

    WITHHELD = "withheld"
    """The PR is a bot's release-train PR, which is landed by release-cut and never handed off."""

    PR_NOT_OPEN = "pr_not_open"
    """The live PR is closed or merged."""

    SUPERSEDED = "superseded"
    """A newer handoff request for the same PR replaced this one while it waited."""

    TIMED_OUT = "timed_out"
    """The wait budget ran out before the live PR was ready."""

    LEDGER_REFUSED = "ledger_refused"
    """The ledger refused or failed the rows; nothing was appended."""

    APPEND_UNCONFIRMED = "append_unconfirmed"
    """Every append attempt ended with no receipt; the rows may be in the ledger under this request id."""


# The refusal codes the decision compute can return, each the suffix of its
# FSM trigger ``evaluated_<code>`` (WAITING to REFUSED).
DECISION_REFUSAL_CODES: frozenset[EnumPrHandoffErrorCode] = frozenset(
    {
        EnumPrHandoffErrorCode.STALE_HEAD,
        EnumPrHandoffErrorCode.MISSING_TICKET,
        EnumPrHandoffErrorCode.NOT_OWNED,
        EnumPrHandoffErrorCode.MISSING_LAB_PROOF,
        EnumPrHandoffErrorCode.HELD,
        EnumPrHandoffErrorCode.WITHHELD,
        EnumPrHandoffErrorCode.PR_NOT_OPEN,
    }
)


__all__: list[str] = ["DECISION_REFUSAL_CODES", "EnumPrHandoffErrorCode"]
