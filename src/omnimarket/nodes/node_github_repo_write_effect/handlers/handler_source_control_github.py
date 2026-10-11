# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnibase_spi ``ProtocolSourceControl`` on the shared GitHub transport (OMN-20912).

The first implementer of the SPI's source-control protocols. Reads send typed
requests through the shared landing transport (``omnimarket.github_landing``),
the send path node_pr_landing_github_effect and the gateway use; writes go
through this node's ``HandlerGithubRepoWriteEffect``, so a protocol caller gets
the same mode, quota floor, expected-head and idempotency guards a bus caller
gets. Nothing shells out to ``gh``.

Served (ProtocolSourceControl): connect, health_check, get_capabilities, close,
list_prs, get_pr, get_ci_status, get_diff. Served (the ProtocolCodeRepository
subset): the same reads, and create_pr through the write effect's pr_create.

Not served, each raising :class:`SourceControlOperationNotSupportedError`:

- merge_pr, enable_auto_merge, admin_merge: landing a PR is
  node_pr_landing_github_effect's job, at the expected head, after the landing
  gates; a second merge path would bypass them.
- create_branch, push_branch, force_push, delete_branch, rebase: refs move with
  git from the lane's worktree (push stays git); no node operation rewrites or
  deletes branches.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from omnibase_spi.contracts.services.contract_source_control_types import (
    ModelBranch,
    ModelCheckRun,
    ModelCIStatus,
    ModelDiff,
    ModelMergeResult,
    ModelPullRequest,
)
from omnibase_spi.protocols.types.protocol_service_types import (
    ProtocolServiceHealthStatus,
)

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    compare_request,
    head_check_runs_request,
    next_page_request,
    pull_request_request,
    pull_requests_request,
)
from omnimarket.github_landing.github_landing_transport import (
    UrllibGithubLandingTransport,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    ModelRepoWriteCompleted,
    ModelRepoWriteRequest,
    ModelSourceControlHealthStatus,
    SourceControlOperationNotSupportedError,
    SourceControlWriteRefusedError,
)
from omnimarket.nodes.node_github_repo_write_effect.protocols import (
    ProtocolGithubRepoWriteTransport,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_SECRET_NAME = "GITHUB_TOKEN"
# A runaway paging guard: 10 pages of 100.
_MAX_PAGES = 10
# The diff patch text is cut here; the counts always cover every file.
_MAX_PATCH_CHARS = 256 * 1024
_FAILED = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
)
_MERGE_REASON = (
    "landing is node_pr_landing_github_effect's job, at the expected head after "
    "the landing gates; a second merge path would bypass them"
)
_REF_REASON = (
    "refs move with git from the lane's worktree; no node operation creates, "
    "rewrites or deletes branches"
)


def _str(raw: object, *path: str) -> str:
    node = raw
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return node if isinstance(node, str) else ""


def _pull_request(raw: dict[str, object]) -> ModelPullRequest:
    merged_at = raw.get("merged_at")
    mergeable = raw.get("mergeable")
    return ModelPullRequest(
        number=int(str(raw.get("number", 0))),
        title=_str(raw, "title"),
        state="merged" if merged_at else _str(raw, "state"),
        author=_str(raw, "user", "login"),
        head_ref=_str(raw, "head", "ref"),
        base_ref=_str(raw, "base", "ref"),
        mergeable=mergeable if isinstance(mergeable, bool) else None,
        url=_str(raw, "html_url"),
        created_at=datetime.fromisoformat(_str(raw, "created_at")),
        updated_at=datetime.fromisoformat(_str(raw, "updated_at")),
    )


def _ci_state(checks: list[ModelCheckRun]) -> str:
    if not checks:
        return "pending"
    if any((c.conclusion or "").lower() in _FAILED for c in checks):
        return "failure"
    if any(c.status.lower() != "completed" for c in checks):
        return "pending"
    return "success"


class HandlerSourceControlGithub:
    """omnibase_spi ``ProtocolSourceControl`` (and the served subset of
    ``ProtocolCodeRepository``) on the shared GitHub transport.

    ``transport`` is injected in tests (a recorded fake); without one,
    :meth:`connect` builds the live transport from the contract-declared
    ``GITHUB_TOKEN`` ref.
    """

    def __init__(
        self,
        transport: ProtocolGithubRepoWriteTransport | None = None,
        *,
        lane: str = "source-control-adapter",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._lane = lane
        self._clock = clock
        self._service_id = uuid4()
        self._writer: HandlerGithubRepoWriteEffect | None = None

    # --- lifecycle ------------------------------------------------------------

    async def connect(self) -> bool:
        if self._transport is None:
            ref = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
            secret = await resolve_api_key_async(ref, env_var_fallback=ref)
            if secret is None or not secret.get_secret_value():
                return False
            self._transport = UrllibGithubLandingTransport(secret)
        self._writer = HandlerGithubRepoWriteEffect(self._transport, clock=self._clock)
        return True

    async def health_check(self) -> ProtocolServiceHealthStatus:
        return ModelSourceControlHealthStatus(
            service_id=self._service_id,
            status="healthy" if self._transport is not None else "unavailable",
            last_check=datetime.fromtimestamp(self._clock(), tz=UTC),
        )

    async def get_capabilities(self) -> list[str]:
        return ["read", "write"]

    async def close(self, timeout_seconds: float = 30.0) -> None:
        self._transport = None
        self._writer = None

    # --- reads ----------------------------------------------------------------

    async def _get(
        self, request: ModelGithubHttpRequest
    ) -> tuple[int, dict[str, object]]:
        if self._transport is None:
            raise RuntimeError("connect() before using the source-control adapter")
        response = await self._transport.send(request)
        if response.status == 404:
            return 404, {}
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f"GitHub {request.method} {request.path} answered HTTP "
                f"{response.status}: {response.message()}"
            )
        return response.status, response.body or {}

    async def _collect(
        self, first: ModelGithubHttpRequest, *, item_key: str, limit: int
    ) -> list[dict[str, object]]:
        if self._transport is None:
            raise RuntimeError("connect() before using the source-control adapter")
        items: list[dict[str, object]] = []
        request: ModelGithubHttpRequest | None = first
        for _page in range(_MAX_PAGES):
            if request is None or len(items) >= limit:
                break
            response = await self._transport.send(request)
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"GitHub GET {request.path} answered HTTP {response.status}: "
                    f"{response.message()}"
                )
            page = (response.body or {}).get(item_key)
            if isinstance(page, list):
                items.extend(item for item in page if isinstance(item, dict))
            request = next_page_request(response)
        return items[:limit]

    async def list_prs(
        self, repo: str, state: str = "open", limit: int = 50
    ) -> list[ModelPullRequest]:
        if state not in ("open", "closed", "all"):
            raise ValueError(f"state must be open, closed or all, got {state!r}")
        wanted: Literal["open", "closed", "all"] = (
            "open" if state == "open" else "closed" if state == "closed" else "all"
        )
        raw = await self._collect(
            pull_requests_request(
                repo, state=wanted, per_page=min(max(limit, 1), LIST_PAGE_SIZE)
            ),
            item_key="value",
            limit=limit,
        )
        return [_pull_request(item) for item in raw]

    async def get_pr(self, repo: str, pr_number: int) -> ModelPullRequest:
        status, body = await self._get(pull_request_request(repo, pr_number))
        if status == 404:
            raise KeyError(f"{repo}#{pr_number}")
        return _pull_request(body)

    async def get_ci_status(self, repo: str, ref: str) -> ModelCIStatus:
        raw = await self._collect(
            head_check_runs_request(repo, ref),
            item_key="check_runs",
            limit=_MAX_PAGES * LIST_PAGE_SIZE,
        )
        checks = [
            ModelCheckRun(
                name=_str(item, "name"),
                status=_str(item, "status"),
                conclusion=_str(item, "conclusion") or None,
                url=_str(item, "html_url") or None,
            )
            for item in raw
        ]
        return ModelCIStatus(state=_ci_state(checks), checks=checks)

    async def get_diff(self, repo: str, base: str, head: str) -> ModelDiff:
        status, body = await self._get(compare_request(repo, base, head))
        if status == 404:
            raise KeyError(f"{repo} {base}...{head}")
        raw_files = body.get("files")
        files = [
            f
            for f in (raw_files if isinstance(raw_files, list) else [])
            if isinstance(f, dict)
        ]
        additions = sum(int(str(f.get("additions", 0))) for f in files)
        deletions = sum(int(str(f.get("deletions", 0))) for f in files)
        patch = "\n".join(
            f"--- {_str(f, 'filename')}\n{_str(f, 'patch')}" for f in files
        )
        return ModelDiff(
            files_changed=len(files),
            additions=additions,
            deletions=deletions,
            patch=patch[:_MAX_PATCH_CHARS],
        )

    # --- writes (through the write effect) --------------------------------------

    async def create_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool = False,
    ) -> ModelPullRequest:
        if self._writer is None:
            raise RuntimeError("connect() before using the source-control adapter")
        key = hashlib.sha256(f"{repo}:{head}:{base}".encode()).hexdigest()[:32]
        result = await self._writer.handle(
            ModelRepoWriteRequest(
                correlation_id=uuid4(),
                operation=EnumRepoWriteOperation.PR_CREATE,
                repo=repo,
                idempotency_key=f"spi-create-{key}",
                requested_by_lane=self._lane,
                title=title,
                body=body,
                head=head,
                base=base,
                draft=draft,
            )
        )
        if (
            not isinstance(result, ModelRepoWriteCompleted)
            or result.mode is not EnumRepoWriteMode.ENFORCE
            or result.pr_number is None
        ):
            raise SourceControlWriteRefusedError(result)
        return await self.get_pr(repo, result.pr_number)

    # --- not served -------------------------------------------------------------

    async def merge_pr(
        self, repo: str, pr_number: int, method: str = "squash"
    ) -> ModelMergeResult:
        raise SourceControlOperationNotSupportedError("merge_pr", _MERGE_REASON)

    async def enable_auto_merge(self, repo: str, pr_number: int) -> bool:
        raise SourceControlOperationNotSupportedError(
            "enable_auto_merge", _MERGE_REASON
        )

    async def admin_merge(
        self, repo: str, pr_number: int, method: str = "squash"
    ) -> ModelMergeResult:
        raise SourceControlOperationNotSupportedError("admin_merge", _MERGE_REASON)

    async def create_branch(
        self, repo: str, branch_name: str, from_ref: str = "main"
    ) -> ModelBranch:
        raise SourceControlOperationNotSupportedError("create_branch", _REF_REASON)

    async def push_branch(
        self, repo: str, branch_name: str, from_ref: str = "main"
    ) -> ModelBranch:
        raise SourceControlOperationNotSupportedError("push_branch", _REF_REASON)

    async def force_push(
        self, repo: str, branch_name: str, target_ref: str
    ) -> ModelBranch:
        raise SourceControlOperationNotSupportedError("force_push", _REF_REASON)

    async def delete_branch(self, repo: str, branch_name: str) -> bool:
        raise SourceControlOperationNotSupportedError("delete_branch", _REF_REASON)

    async def rebase(
        self, repo: str, branch_name: str, onto: str = "main"
    ) -> ModelBranch:
        raise SourceControlOperationNotSupportedError("rebase", _REF_REASON)


__all__: list[str] = ["HandlerSourceControlGithub"]
