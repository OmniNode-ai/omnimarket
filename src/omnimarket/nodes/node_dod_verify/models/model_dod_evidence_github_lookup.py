# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed request/result models for the dod-evidence GitHub-lookup EFFECT.

OMN-14400 (RSD-1 of OMN-14398): the I/O boundary types carved out of
``EvidenceCollector``'s 4 gh-CLI subprocess methods (``_lookup_pr_for_ticket``,
``_lookup_repo_for_ticket``, ``_fetch_pr_merge_state``,
``_fetch_pr_checks_green``). Behavior is unchanged — same ``gh`` invocations,
same JSON parsing — only the *shape* of the I/O boundary changes: a canonical
EFFECT handler (``HandlerDodEvidenceGithubEffect``) instead of methods on a
bespoke service class (CLAUDE.md rule 7a).
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class EnumDodEvidenceGithubOperation(StrEnum):
    """The 4 gh-CLI lookups this EFFECT handler performs."""

    LOOKUP_PR_FOR_TICKET = "lookup_pr_for_ticket"
    LOOKUP_REPO_FOR_TICKET = "lookup_repo_for_ticket"
    FETCH_PR_MERGE_STATE = "fetch_pr_merge_state"
    FETCH_PR_CHECKS_GREEN = "fetch_pr_checks_green"


class ModelDodEvidenceGithubLookupCommand(BaseModel):
    """Discriminated request for one of the 4 GitHub lookups."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: EnumDodEvidenceGithubOperation
    correlation_id: UUID = Field(default_factory=uuid4)
    ticket_id: str | None = Field(
        default=None,
        description="Required for LOOKUP_PR_FOR_TICKET / LOOKUP_REPO_FOR_TICKET.",
    )
    repo: str | None = Field(
        default=None,
        description=(
            "owner/repo; required for FETCH_PR_MERGE_STATE / FETCH_PR_CHECKS_GREEN. "
            "Also accepted (and, as of OMN-15382, required to run any `gh` search) "
            "for LOOKUP_PR_FOR_TICKET, so the search is explicitly scoped instead of "
            "resolving whatever repo the process cwd's git remote happens to point at."
        ),
    )
    pr_number: int | None = Field(
        default=None,
        description="Required for FETCH_PR_MERGE_STATE / FETCH_PR_CHECKS_GREEN.",
    )


class ModelDodEvidenceGithubLookupResultEvent(BaseModel):
    """Resolved result for the requested operation.

    Only the fields relevant to ``operation`` are populated; the rest keep
    their defaults. This mirrors the discriminated-union shape of the 4
    original method return types without needing 4 separate result models.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    operation: EnumDodEvidenceGithubOperation

    # LOOKUP_PR_FOR_TICKET / LOOKUP_REPO_FOR_TICKET
    text_value: str = Field(
        default="",
        description="Resolved PR number or owner/repo string; empty when unresolved.",
    )

    # FETCH_PR_MERGE_STATE
    resolved: bool = Field(
        default=True,
        description=(
            "False when FETCH_PR_MERGE_STATE could not resolve the PR at all "
            "(gh missing/auth/network/not-found) — the fail-closed signal the "
            "original method conveyed by returning None."
        ),
    )
    merged: bool | None = Field(default=None)
    state: str | None = Field(default=None)

    # FETCH_PR_CHECKS_GREEN
    checks_green: bool | None = Field(default=None)
    detail: str | None = Field(default=None)

    # LOOKUP_PR_FOR_TICKET / LOOKUP_REPO_FOR_TICKET failure classification
    # (OMN-15382). Set only when ``text_value`` is empty; ``None`` on success
    # or for other operations. ``*_LOOKUP_AMBIGUOUS`` means more than one
    # candidate survived exact-ticket-token filtering (never silently take
    # the most recent); ``*_LOOKUP_FAILED`` covers zero candidates, missing
    # required ``repo`` scoping, or a gh/transport/parse error.
    error_code: str | None = Field(
        default=None,
        description=(
            "PR_LOOKUP_FAILED | PR_LOOKUP_AMBIGUOUS | REPO_LOOKUP_FAILED | "
            "REPO_LOOKUP_AMBIGUOUS, else None."
        ),
    )


__all__ = [
    "EnumDodEvidenceGithubOperation",
    "ModelDodEvidenceGithubLookupCommand",
    "ModelDodEvidenceGithubLookupResultEvent",
]
