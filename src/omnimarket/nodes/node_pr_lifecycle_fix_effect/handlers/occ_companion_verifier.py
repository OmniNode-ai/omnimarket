# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Independent read-back verifier for pushed OCC Evidence-Source companions.

OMN-14173. The ``receipt_evidence_source_autobind`` arm used to increment
``prs_fixed`` on the strength of the adapter call returning without raising —
which includes the no-op idempotency return and any short-circuit path that
pushes nothing. Dogfooding ``merge_sweep --fix-only`` on omnimarket #1651/#1652
returned ``prs_fixed=2`` while authoring ZERO OCC companions: a false-success of
the exact class the tooling is meant to catch.

This verifier proves the EFFECT, not the call. After the autobind adapter runs,
it re-reads GitHub and confirms all three of:

  1. the product PR body carries ``Evidence-Source: OCC#<n>`` (an OCC source, not
     a bare product-head SHA);
  2. OCC PR ``#<n>`` is in the ``open`` state on ``onex_change_control``;
  3. the expected ``auto/<repo>-pr-<n>-occ-autobind`` companion branch exists on
     the OCC remote.

Any missing evidence, resolution error, or token failure fails CLOSED
(``verified=False``) — the orchestrator then reports the PR as NOT fixed rather
than counting a phantom companion.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile

from omnimarket.github_api import GitHubApiError, rest_json, split_repo
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    _resolve_github_token,
)

# OMN-14189: the OCC-source read-back uses the single Piece-2 parser seam, not a
# local Evidence-Source regex — same source of truth as the emitter and gate.
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_stamp_authoring import (
    product_pr_occ_binding,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelOccCompanionVerification,
)
from omnimarket.occ_git_transport import (
    OCC_REPO,
    authenticated_occ_url,
    run_git,
)

logger = logging.getLogger(__name__)

_GIT_LS_REMOTE_TIMEOUT_SECONDS = 60


def _expected_branch(repo: str, pr_number: int) -> str:
    """Return the deterministic auto/* companion branch the adapter pushes.

    Mirrors ``OccCompanionEmitter`` exactly (one OCC branch per product PR):
    ``auto/<repo-slug-lower>-pr-<pr_number>-occ-autobind``.
    """
    repo_slug = repo.replace("/", "-").lower()
    return f"auto/{repo_slug}-pr-{pr_number}-occ-autobind"


class OccCompanionVerifier:
    """Confirm a pushed OCC Evidence-Source companion via GitHub read-back.

    Fail-closed: every branch returns ``verified=False`` unless all three
    independent facts are observed. Never raises — a resolution/network error
    is itself a verification failure (the effect could not be proven).
    """

    def __init__(self, *, occ_repo: str = OCC_REPO) -> None:
        self._occ_repo = occ_repo

    async def verify_companion(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccCompanionVerification:
        return await asyncio.to_thread(self._verify_sync, repo, pr_number)

    def _verify_sync(self, repo: str, pr_number: int) -> ModelOccCompanionVerification:
        expected_branch = _expected_branch(repo, pr_number)
        try:
            token = _resolve_github_token()
        except Exception as exc:
            return ModelOccCompanionVerification(
                verified=False,
                occ_branch=expected_branch,
                detail=f"could not resolve GitHub token: {exc}",
            )

        # 1. Product PR body must carry Evidence-Source: OCC#<n>.
        try:
            owner, repo_name = split_repo(repo)
            pr_data = rest_json(
                "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
            )
        except (GitHubApiError, ValueError) as exc:
            return ModelOccCompanionVerification(
                verified=False,
                occ_branch=expected_branch,
                detail=f"could not read product PR {repo}#{pr_number}: {exc}",
            )
        body = pr_data.get("body") or ""
        occ_pr_number = product_pr_occ_binding(body)
        if occ_pr_number is None:
            return ModelOccCompanionVerification(
                verified=False,
                occ_branch=expected_branch,
                evidence_source_present=False,
                detail=(
                    f"{repo}#{pr_number} body has no `Evidence-Source: OCC#<n>` "
                    "(companion not bound)"
                ),
            )

        # 2. The referenced OCC companion PR must be OPEN.
        try:
            occ_owner, occ_name = split_repo(self._occ_repo)
            occ_pr = rest_json(
                "GET",
                f"/repos/{occ_owner}/{occ_name}/pulls/{occ_pr_number}",
                token=token,
            )
        except (GitHubApiError, ValueError) as exc:
            return ModelOccCompanionVerification(
                verified=False,
                occ_pr_number=occ_pr_number,
                occ_branch=expected_branch,
                evidence_source_present=True,
                detail=f"could not read OCC PR #{occ_pr_number}: {exc}",
            )
        occ_pr_open = (occ_pr.get("state") or "") == "open"

        # 3. The auto/* companion branch must exist on the OCC remote.
        branch_exists = self._branch_exists(token, expected_branch)

        verified = bool(occ_pr_open and branch_exists)
        detail = (
            f"verified OCC#{occ_pr_number} companion for {repo}#{pr_number}"
            if verified
            else (
                f"companion incomplete for {repo}#{pr_number}: "
                f"occ_pr_open={occ_pr_open} branch_exists={branch_exists}"
            )
        )
        return ModelOccCompanionVerification(
            verified=verified,
            occ_pr_number=occ_pr_number,
            occ_branch=expected_branch,
            evidence_source_present=True,
            occ_pr_open=occ_pr_open,
            branch_exists=branch_exists,
            detail=detail,
        )

    def _branch_exists(self, token: str, branch: str) -> bool:
        """True iff ``git ls-remote --heads`` finds the branch on the OCC remote."""
        try:
            with tempfile.TemporaryDirectory(prefix="occ-verify-") as tmpdir:
                out = run_git(
                    [
                        "git",
                        "ls-remote",
                        "--heads",
                        authenticated_occ_url(token, self._occ_repo),
                        branch,
                    ],
                    cwd=tmpdir,
                    timeout=_GIT_LS_REMOTE_TIMEOUT_SECONDS,
                )
        except Exception as exc:
            logger.warning("occ_companion_verifier: ls-remote failed: %s", exc)
            return False
        return bool(out.strip())


__all__ = ["OccCompanionVerifier"]
