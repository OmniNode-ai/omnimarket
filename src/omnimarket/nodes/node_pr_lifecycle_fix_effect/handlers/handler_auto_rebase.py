# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAutoRebase — auto-rebase stale PR branches via GitHub's update-branch API.

Targets Track A-update PRs (merge_state_status=BEHIND or UNKNOWN).
Protocol-injected adapter allows mock substitution in tests with zero infra.

Related:
    - OMN-8204: Task 7 — Add HandlerAutoRebase to node_pr_lifecycle_fix_effect
    - OMN-19831: the live adapter's REST calls go through the shared landing
      transport (``omnimarket.github_landing``), the same request builders and
      send path node_pr_landing_github_effect uses.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr

from omnimarket.github_landing.github_landing_requests import (
    pull_request_request,
    update_branch_request,
)
from omnimarket.github_landing.github_landing_transport import (
    UrllibGithubLandingTransport,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def _resolve_github_token() -> SecretStr:
    """Resolve the GitHub token from the contract-declared ref (OMN-12856).

    ``env_var_fallback`` (OMN-14452): the deployed lane's secret resolver is
    LLM/Slack-scoped with convention fallback disabled and never resolves
    ``GITHUB_TOKEN`` — falling back to the literal env var (already passed
    straight through as a container env var) resolves it instead of raising.
    """
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret


# ---------------------------------------------------------------------------
# Request / Result models
# ---------------------------------------------------------------------------


class ModelRebaseRequest(BaseModel):
    """Request to auto-rebase a single stale PR branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    dry_run: bool = False


class ModelRebaseResult(BaseModel):
    """Result of a single PR auto-rebase attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    success: bool
    error_message: str | None = None
    rebase_sha: str | None = None


# ---------------------------------------------------------------------------
# Adapter protocol — injected at construction; swapped for mocks in tests
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolRebaseAdapter(Protocol):
    """Minimal GitHub operations required by the auto-rebase handler."""

    async def update_branch(self, repo: str, pr_number: int) -> str:
        """Update (rebase) a PR branch against its base. Returns new HEAD SHA or action string."""
        ...


# ---------------------------------------------------------------------------
# Default live adapter (GitHub REST API)
# ---------------------------------------------------------------------------


class _LiveRebaseAdapter:
    """Live adapter that calls GitHub's update-branch API."""

    async def update_branch(self, repo: str, pr_number: int) -> str:
        return await asyncio.to_thread(self._update_branch_sync, repo, pr_number)

    def _update_branch_sync(self, repo: str, pr_number: int) -> str:
        transport = UrllibGithubLandingTransport(_resolve_github_token())
        pr = _send_json(transport, pull_request_request(repo, pr_number))
        head = pr.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not head_sha:
            raise RuntimeError(
                f"update-branch failed for {repo}#{pr_number}: missing head sha"
            )
        _send_json(transport, update_branch_request(repo, pr_number, head_sha))
        refreshed = _send_json(transport, pull_request_request(repo, pr_number))
        refreshed_head = refreshed.get("head")
        new_sha = (
            refreshed_head.get("sha") if isinstance(refreshed_head, dict) else None
        )
        if isinstance(new_sha, str) and new_sha:
            return new_sha
        return f"rebased {repo}#{pr_number}"


def _send_json(
    transport: UrllibGithubLandingTransport, request: ModelGithubHttpRequest
) -> dict[str, object]:
    """Send one REST call; raise with GitHub's message on a non-2xx answer."""
    response = transport.send_sync(request)
    if not 200 <= response.status < 300:
        raise RuntimeError(
            f"GitHub {request.method} {request.path} returned HTTP "
            f"{response.status}: {response.message()}"
        )
    return dict(response.body or {})


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerAutoRebase:
    """Auto-rebase stale PR branches via the GitHub update-branch API.

    In dry_run=True: logs intent, returns ModelRebaseResult(success=True) without
    calling gh.
    """

    def __init__(self, adapter: ProtocolRebaseAdapter | None = None) -> None:
        self._adapter: ProtocolRebaseAdapter = adapter or _LiveRebaseAdapter()

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "EFFECT"

    @property
    def correlation_id(self) -> UUID | None:
        return None

    async def handle(self, payload: ModelRebaseRequest) -> ModelRebaseResult:
        """Rebase a stale PR branch.

        Args:
            payload: Typed request carrying pr_number, repo, and dry_run.

        Returns:
            ModelRebaseResult indicating success or failure.
        """
        pr_number = payload.pr_number
        repo = payload.repo
        dry_run = payload.dry_run

        logger.info(
            "auto-rebase: pr=%s repo=%s dry_run=%s",
            pr_number,
            repo,
            dry_run,
        )

        if dry_run:
            logger.info(
                "[noop] would rebase branch for %s#%s via update-branch API",
                repo,
                pr_number,
            )
            return ModelRebaseResult(pr_number=pr_number, repo=repo, success=True)

        try:
            sha = await self._adapter.update_branch(repo=repo, pr_number=pr_number)
            logger.info(
                "auto-rebase succeeded: pr=%s repo=%s sha=%s",
                pr_number,
                repo,
                sha,
            )
            return ModelRebaseResult(
                pr_number=pr_number, repo=repo, success=True, rebase_sha=sha
            )
        except Exception as exc:
            logger.warning(
                "auto-rebase failed: pr=%s repo=%s error=%s",
                pr_number,
                repo,
                exc,
                exc_info=True,
            )
            return ModelRebaseResult(
                pr_number=pr_number,
                repo=repo,
                success=False,
                error_message=str(exc),
            )


__all__: list[str] = [
    "HandlerAutoRebase",
    "ModelRebaseRequest",
    "ModelRebaseResult",
    "ProtocolRebaseAdapter",
]
