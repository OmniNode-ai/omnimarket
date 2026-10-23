# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read back current-head evidence from the product PR after a repair.

Only the product repository is read. The repository evidence gates validate
its contracts; no companion PR or companion stamp participates in the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_GH_VIEW_TIMEOUT_SECONDS = 30


class ModelPrEvidenceReadbackResult(BaseModel):
    """Independent current-head evidence readback for a product PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: bool
    head_sha: str | None = None
    evidence_tickets: tuple[str, ...] = ()
    evidence_source_present: bool = False
    reason: str = ""


@runtime_checkable
class ProtocolPrJsonFetcher(Protocol):
    """Fetch a PR's raw JSON. Injected so the read-back is hermetic in tests.

    Implementations return a mapping that MUST include a ``body`` (str) and a
    ``state`` (str; ``open`` / ``closed`` / ``merged``, case-insensitive). Raise
    on any resolution error — the read-back treats a raise as fail-closed.
    """

    def fetch_pr(self, repo: str, pr_number: int) -> dict[str, object]: ...


@runtime_checkable
class ProtocolPrEvidenceReadback(Protocol):
    """Independent readback of the product repository evidence checks."""

    async def verify_fix_landed(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelPrEvidenceReadbackResult:
        """Read the product PR back and require passing repository evidence."""
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
                "body,title,state,number,headRefOid,statusCheckRollup",
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


class PrEvidenceReadback:
    """Verify repository evidence checks on the current product PR head.

    The injected effect returns one current-head rollup. Required contexts
    must all be present and successful; a duplicate failing run still refuses.
    No companion repository or stamp is read, and failures remain unverified.
    """

    def __init__(
        self,
        *,
        fetcher: ProtocolPrJsonFetcher | None = None,
    ) -> None:
        self._fetcher: ProtocolPrJsonFetcher = fetcher or _GhCliPrFetcher()

    async def verify_fix_landed(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelPrEvidenceReadbackResult:
        return await asyncio.to_thread(self._verify_sync, repo, pr_number, ticket_id)

    def _verify_sync(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> ModelPrEvidenceReadbackResult:
        try:
            product_pr = self._fetcher.fetch_pr(repo, pr_number)
        except Exception as exc:
            return ModelPrEvidenceReadbackResult(
                verified=False,
                reason=f"could not read product PR {repo}#{pr_number}: {exc}",
            )
        head = product_pr.get("headRefOid")
        if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None:
            return ModelPrEvidenceReadbackResult(
                verified=False, reason="product PR has no full current head"
            )
        text = f"{product_pr.get('title') or ''}\n{product_pr.get('body') or ''}"
        tickets = tuple(dict.fromkeys(re.findall(r"\bOMN-\d+\b", text.upper())))
        if ticket_id is not None and ticket_id.strip().upper() not in tickets:
            return ModelPrEvidenceReadbackResult(
                verified=False, reason="product PR does not name the expected ticket"
            )
        raw_checks = product_pr.get("statusCheckRollup")
        checks = raw_checks if isinstance(raw_checks, list) else []
        required = {"repo-evidence / verify", "repo-evidence / dod-verify"}
        relevant = [
            check
            for check in checks
            if isinstance(check, dict) and check.get("name") in required
        ]
        present = {check.get("name") for check in relevant}
        verified = (
            str(product_pr.get("state") or "").lower() == "open"
            and present == required
            and all(
                check.get("status") == "COMPLETED"
                and check.get("conclusion") == "SUCCESS"
                for check in relevant
            )
        )
        return ModelPrEvidenceReadbackResult(
            verified=verified,
            head_sha=head,
            evidence_tickets=tickets,
            evidence_source_present=present == required,
            reason=(
                "current product-head repository evidence checks passed"
                if verified
                else "current product-head repository evidence checks are absent or not passing"
            ),
        )


class _UnverifiedPrEvidenceReadback:
    """Fail-closed default — proves nothing, so counts nothing (OMN-14191).

    Used when no live read-back is wired (direct ``_dispatch_fix_parallel`` unit
    tests, dry-run, or the runtime-boot path before ``_ensure_sub_handlers``
    injects the live :class:`PrEvidenceReadback`). A missing read-back UNDER-counts
    ``prs_fixed`` — never falsely counts — which is the safe direction.
    """

    async def verify_fix_landed(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelPrEvidenceReadbackResult:
        return ModelPrEvidenceReadbackResult(
            verified=False,
            reason=(
                "no product evidence readback wired; fail-closed (cannot prove the fix "
                "passed its repository checks)"
            ),
        )


__all__ = [
    "ModelPrEvidenceReadbackResult",
    "PrEvidenceReadback",
    "ProtocolPrEvidenceReadback",
    "ProtocolPrJsonFetcher",
    "_UnverifiedPrEvidenceReadback",
]
