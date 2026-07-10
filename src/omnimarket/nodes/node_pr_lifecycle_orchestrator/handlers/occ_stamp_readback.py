# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Independent OCC-stamp read-back gate for merge-sweep ``prs_fixed`` accounting.

OMN-14191 (Piece 5/5 of the OMN-14180 canonical OCC evidence stamp-model). The
merge-sweep orchestrator must NEVER count ``prs_fixed`` on DISPATCH. After a fix
arm runs, the orchestrator reads back the ACTUAL pushed OCC companion PR *and*
the product PR body and confirms the OCC stamp landed — parsing the product body
via the Piece-2 canonical parser (``omnibase_compat.contracts.pr_occ_stamp``,
the SAME parser the receipt gate uses — no second extraction path) — before
counting the fix.

This generalizes the OMN-14173 ``OccCompanionVerifier`` read-back (which gated
only the OCC-autobind arm) to EVERY fix arm, closing OMN-14174's
dispatch-vs-effect over-count: mixed-failure PRs that fell through to
``CODE_FAILURE`` / ``CI_FAILURE`` / ``CODERABBIT`` / ``CONFLICT`` used to set
``fix_applied=True`` on dispatch and inflate ``prs_fixed`` with zero landed
effect.

CLAUDE.md Rule 3 (never trust an effect's self-report): the read-back uses live
``gh``/remote state via an injected fetcher, never the fix handler's returned
``fix_applied`` / ``occ_companion_verified`` flag. Fail-closed — a missing stamp,
an absent/closed OCC companion PR, or any resolution error yields
``verified=False``, so a noop / short-circuited dispatch can never count.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Protocol, runtime_checkable

# Piece-2 canonical OCC-stamp parser (OMN-14188), relocated to the lowest shared
# layer, omnibase_compat, under OMN-14223 so every repo consumes one definition.
# Single import block — the only cross-repo schema surface this node depends on;
# ships in omnibase-compat >= 0.5.6 and resolves once 0.5.6 is published to PyPI
# + uv.lock is refreshed (OMN-14180 critical-path).
from omnibase_compat.contracts.pr_occ_stamp import (
    EnumPrEvidenceSourceKind,
    parse_pr_occ_metadata_stamp,
)
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# OCC companion repo. ``Evidence-Source: OCC#<n>`` references a PR on this repo.
_OCC_REPO = "OmniNode-ai/onex_change_control"

_GH_VIEW_TIMEOUT_SECONDS = 30


class ModelOccStampReadbackResult(BaseModel):
    """Independent read-back proof that a fix arm's OCC stamp actually landed.

    ``verified`` is True only when a live read-back confirms BOTH halves of the
    canonical stamp (OMN-14191 acceptance): (a) the referenced OCC companion PR
    exists on the remote and is open, and (b) the product PR body carries the
    ``Evidence-Source: OCC#<n>`` + ``Evidence-Ticket: OMN-<n>`` stamp (parsed via
    the Piece-2 canonical parser). Fails closed on any missing evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: bool = Field(
        ...,
        description=(
            "True only when the pushed OCC companion + product-PR Evidence stamp "
            "are independently confirmed by a live gh/remote read-back."
        ),
    )
    occ_pr_number: int | None = Field(
        default=None,
        description="OCC companion PR number parsed from the product PR body.",
    )
    evidence_tickets: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Evidence-Ticket ids parsed from the product PR body.",
    )
    evidence_source_present: bool = Field(
        default=False,
        description="Product PR body carries an `Evidence-Source: OCC#<n>` stamp.",
    )
    occ_pr_open: bool = Field(
        default=False,
        description="The referenced OCC companion PR is open on the remote.",
    )
    reason: str = Field(
        default="",
        description="Human-readable read-back detail (specific reason on failure).",
    )


@runtime_checkable
class ProtocolPrJsonFetcher(Protocol):
    """Fetch a PR's raw JSON. Injected so the read-back is hermetic in tests.

    Implementations return a mapping that MUST include a ``body`` (str) and a
    ``state`` (str; ``open`` / ``closed`` / ``merged``, case-insensitive). Raise
    on any resolution error — the read-back treats a raise as fail-closed.
    """

    def fetch_pr(self, repo: str, pr_number: int) -> dict[str, object]: ...


@runtime_checkable
class ProtocolOccStampReadback(Protocol):
    """Independent read-back of a fix arm's landed OCC stamp (OMN-14191)."""

    async def verify_fix_landed(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccStampReadbackResult:
        """Read gh/remote back; return whether the OCC stamp actually landed."""
        ...


class _GhCliPrFetcher:
    """Live ``gh pr view`` fetcher — mirrors the orchestrator's existing gh idiom.

    Uses the ``gh`` CLI (which owns its own auth) rather than a resolved token,
    matching ``HandlerPrLifecycleOrchestrator._enumerate_open_pr_numbers`` and
    avoiding a cross-node import of the fix-effect token resolver.
    """

    def fetch_pr(self, repo: str, pr_number: int) -> dict[str, object]:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "body,state,number",
            ],
            capture_output=True,
            text=True,
            timeout=_GH_VIEW_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"gh pr view {repo}#{pr_number} failed "
                f"(returncode={proc.returncode}): "
                f"{proc.stderr.strip() or '<no stderr>'}"
            )
        parsed = json.loads(proc.stdout)
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"unexpected gh pr view JSON for {repo}#{pr_number}: {type(parsed)}"
            )
        return parsed


class OccStampReadback:
    """Confirm a fix arm's landed OCC stamp via a live gh/remote read-back.

    Fail-closed: every path returns ``verified=False`` unless the product PR body
    carries the canonical ``Evidence-Source: OCC#<n>`` + ``Evidence-Ticket`` stamp
    (Piece-2 parse) AND the referenced OCC companion PR exists and is open. Never
    raises — a resolution/network error is itself a verification failure.
    """

    def __init__(
        self,
        *,
        fetcher: ProtocolPrJsonFetcher | None = None,
        occ_repo: str = _OCC_REPO,
    ) -> None:
        self._fetcher: ProtocolPrJsonFetcher = fetcher or _GhCliPrFetcher()
        self._occ_repo = occ_repo

    async def verify_fix_landed(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccStampReadbackResult:
        return await asyncio.to_thread(self._verify_sync, repo, pr_number, ticket_id)

    def _verify_sync(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> ModelOccStampReadbackResult:
        # (b) Read the product PR body back and parse it with the Piece-2 parser.
        try:
            product_pr = self._fetcher.fetch_pr(repo, pr_number)
        except Exception as exc:
            return ModelOccStampReadbackResult(
                verified=False,
                reason=f"could not read product PR {repo}#{pr_number}: {exc}",
            )
        body = str(product_pr.get("body") or "")
        stamp = parse_pr_occ_metadata_stamp(body, repo=repo, pr_number=pr_number)
        source = stamp.evidence_source
        tickets = stamp.evidence_tickets

        if (
            source is None
            or source.kind is not EnumPrEvidenceSourceKind.OCC_PR
            or source.occ_pr_number is None
        ):
            return ModelOccStampReadbackResult(
                verified=False,
                evidence_tickets=tickets,
                reason=(
                    f"{repo}#{pr_number} body has no `Evidence-Source: OCC#<n>` "
                    "stamp (no OCC companion landed on dispatch)"
                ),
            )
        occ_pr_number = source.occ_pr_number

        if not tickets:
            return ModelOccStampReadbackResult(
                verified=False,
                occ_pr_number=occ_pr_number,
                evidence_source_present=True,
                reason=(
                    f"{repo}#{pr_number} body carries no `Evidence-Ticket: OMN-<n>` "
                    "stamp (incomplete OCC evidence)"
                ),
            )

        # Consistency: when the fixer targeted a specific ticket, the landed
        # stamp must reference it — "the expected Evidence content" (AC (a)).
        if ticket_id is not None and ticket_id.strip().upper() not in tickets:
            return ModelOccStampReadbackResult(
                verified=False,
                occ_pr_number=occ_pr_number,
                evidence_source_present=True,
                evidence_tickets=tickets,
                reason=(
                    f"{repo}#{pr_number} Evidence-Ticket {list(tickets)} does not "
                    f"include the fixed ticket {ticket_id!r}"
                ),
            )

        # (a) Read the OCC companion PR back — it must exist and be open.
        try:
            occ_pr = self._fetcher.fetch_pr(self._occ_repo, occ_pr_number)
        except Exception as exc:
            return ModelOccStampReadbackResult(
                verified=False,
                occ_pr_number=occ_pr_number,
                evidence_source_present=True,
                evidence_tickets=tickets,
                reason=f"could not read OCC companion PR #{occ_pr_number}: {exc}",
            )
        occ_state = str(occ_pr.get("state") or "").lower()
        occ_pr_open = occ_state == "open"

        return ModelOccStampReadbackResult(
            verified=occ_pr_open,
            occ_pr_number=occ_pr_number,
            evidence_tickets=tickets,
            evidence_source_present=True,
            occ_pr_open=occ_pr_open,
            reason=(
                f"OCC stamp landed: {repo}#{pr_number} -> OCC#{occ_pr_number} (open)"
                if occ_pr_open
                else (
                    f"OCC companion PR #{occ_pr_number} is not open "
                    f"(state={occ_state or 'unknown'})"
                )
            ),
        )


class _UnverifiedOccStampReadback:
    """Fail-closed default — proves nothing, so counts nothing (OMN-14191).

    Used when no live read-back is wired (direct ``_dispatch_fix_parallel`` unit
    tests, dry-run, or the runtime-boot path before ``_ensure_sub_handlers``
    injects the live :class:`OccStampReadback`). A missing read-back UNDER-counts
    ``prs_fixed`` — never falsely counts — which is the safe direction.
    """

    async def verify_fix_landed(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccStampReadbackResult:
        return ModelOccStampReadbackResult(
            verified=False,
            reason=(
                "no OCC-stamp read-back wired; fail-closed (cannot prove the fix "
                "landed an OCC companion)"
            ),
        )


__all__ = [
    "ModelOccStampReadbackResult",
    "OccStampReadback",
    "ProtocolOccStampReadback",
    "ProtocolPrJsonFetcher",
    "_UnverifiedOccStampReadback",
]
