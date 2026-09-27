# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_ci_rerun_effect [OMN-8962].

EFFECT node. Serial-in-handler execution per Phase 1 audit.
Triggers GitHub's rerun-failed-jobs API for the PR's most recent failed workflow
run. Only reruns failed jobs; does not retrigger successful ones.

The GitHub token is resolved at handle() time from the contract-declared
``api_key_ref`` (``GITHUB_TOKEN``) — no direct ``os.environ`` read.

OMN-19831: every REST call goes through the shared landing transport
(``omnimarket.github_landing``), the same request builders and send path
node_pr_landing_github_effect uses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from pydantic import SecretStr

from omnimarket.github_landing.github_landing_requests import (
    GithubLandingRequestError,
    create_commit_request,
    fast_forward_ref_request,
    git_commit_request,
    git_ref_request,
    rerun_failed_jobs_request,
    split_repository,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
    UrllibGithubLandingTransport,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_ci_rerun_effect.models.model_ci_rerun_triggered_event import (
    ModelCiRerunTriggeredEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelCiRerunCommand,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class HandlerCiRerunEffect:
    """EFFECT: trigger the GitHub rerun-failed-jobs API on a PR's failing run.

    Canonical thin shape (OMN-14242): ``handle()`` takes a single typed
    ``ModelCiRerunCommand`` payload and returns the typed
    ``ModelCiRerunTriggeredEvent`` result directly — no envelope, no
    ``ModelHandlerOutput`` wrapper, no coercion; the runtime wraps.
    """

    async def handle(self, request: ModelCiRerunCommand) -> ModelCiRerunTriggeredEvent:
        """Trigger CI rerun. Real work runs inline before returning.

        The GitHub token ref-name is sourced from the contract ``secrets`` block
        (OMN-12856) and resolved at the effect boundary via the canonical
        secret-store resolver — never read from env directly in this handler.
        """
        _github_ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
        github_secret = await resolve_api_key_async(_github_ref)
        if github_secret is None:
            raise RuntimeError(
                f"api_key_ref {_github_ref!r} resolved to None — "
                "ensure GITHUB_TOKEN is set in the secret store."
            )
        token = github_secret.get_secret_value()

        t0 = time.monotonic()
        triggered: bool
        error: str | None
        if request.retrigger_mode == "empty_commit":
            # OMN-13416 — a required workflow produced 0 runs on HEAD (GitHub
            # dropped the dispatch event). There is no run to rerun; push an
            # empty commit on the head branch to re-fire the dropped events.
            if not request.head_branch:
                triggered, error = False, "empty_commit requires head_branch"
            elif not request.head_sha:
                triggered, error = False, "empty_commit requires head_sha"
            else:
                triggered, error = await self._empty_commit(
                    request.repo,
                    request.head_branch,
                    request.head_sha,
                    request.pr_number,
                    token,
                )
        else:
            if not request.run_id_github:
                triggered, error = False, "rerun_failed requires run_id_github"
            else:
                triggered, error = await self._rerun(
                    request.run_id_github, request.repo, token
                )
        elapsed = time.monotonic() - t0

        if triggered:
            _log.info(
                "CI re-trigger (%s): %s#%s run=%s branch=%s (elapsed=%.2fs)",
                request.retrigger_mode,
                request.repo,
                request.pr_number,
                request.run_id_github,
                request.head_branch,
                elapsed,
            )
        else:
            _log.error(
                "CI re-trigger (%s) FAILED: %s#%s run=%s branch=%s error=%r "
                "(elapsed=%.2fs)",
                request.retrigger_mode,
                request.repo,
                request.pr_number,
                request.run_id_github,
                request.head_branch,
                error,
                elapsed,
            )

        return ModelCiRerunTriggeredEvent(
            pr_number=request.pr_number,
            repo=request.repo,
            correlation_id=request.correlation_id,
            run_id=request.run_id,
            total_prs=request.total_prs,
            run_id_github=request.run_id_github,
            rerun_triggered=triggered,
            error=error,
            elapsed_seconds=elapsed,
        )

    async def _rerun(
        self, run_id_github: str, repo: str, token: str
    ) -> tuple[bool, str | None]:
        """Trigger GitHub's rerun-failed-jobs workflow-run API."""
        return await asyncio.to_thread(self._rerun_sync, run_id_github, repo, token)

    def _rerun_sync(
        self, run_id_github: str, repo: str, token: str
    ) -> tuple[bool, str | None]:
        try:
            split_repository(repo)
        except GithubLandingRequestError:
            return False, f"invalid repo slug: {repo!r}"
        if not run_id_github.isdigit() or int(run_id_github) <= 0:
            return False, f"invalid run id: {run_id_github!r}"
        transport = UrllibGithubLandingTransport(SecretStr(token))
        try:
            response = transport.send_sync(
                rerun_failed_jobs_request(repo, int(run_id_github))
            )
        except GithubLandingTransportError as exc:
            return False, str(exc)
        if 200 <= response.status < 300:
            return True, None
        return False, response.message()

    async def _empty_commit(
        self,
        repo: str,
        head_branch: str,
        expected_head_sha: str,
        pr_number: int,
        token: str,
    ) -> tuple[bool, str | None]:
        """Push an empty commit on ``head_branch`` to re-trigger CI (OMN-13416)."""
        return await asyncio.to_thread(
            self._empty_commit_sync,
            repo,
            head_branch,
            expected_head_sha,
            pr_number,
            token,
        )

    def _empty_commit_sync(
        self,
        repo: str,
        head_branch: str,
        expected_head_sha: str,
        pr_number: int,
        token: str,
    ) -> tuple[bool, str | None]:
        """Create an empty commit on the head branch via the Git Data API.

        Reuses HEAD's tree (zero file change) with HEAD as the single parent,
        then fast-forwards the branch ref. This re-fires the workflow-dispatch
        events that GitHub dropped without altering any content.
        """
        try:
            split_repository(repo)
        except GithubLandingRequestError:
            return False, f"invalid repo slug: {repo!r}"
        if not head_branch:
            return False, "empty head_branch for empty_commit re-trigger"
        if not expected_head_sha:
            return False, "empty expected_head_sha for empty_commit re-trigger"

        transport = UrllibGithubLandingTransport(SecretStr(token))
        try:
            # 1. Resolve the branch's current commit SHA.
            ref = self._api_json(transport, git_ref_request(repo, head_branch))
            ref_object = ref.get("object")
            head_sha = ref_object.get("sha") if isinstance(ref_object, dict) else None
            if not head_sha:
                return False, f"could not resolve head SHA for {head_branch}"
            if head_sha != expected_head_sha:
                return (
                    False,
                    f"head branch moved: expected {expected_head_sha}, got {head_sha}",
                )

            # 2. Read the head commit to reuse its tree (empty diff).
            head_commit = self._api_json(transport, git_commit_request(repo, head_sha))
            commit_tree = head_commit.get("tree")
            tree_sha = commit_tree.get("sha") if isinstance(commit_tree, dict) else None
            if not tree_sha:
                return False, f"could not resolve tree SHA for {head_sha}"

            # 3. Create a new commit pointing at the same tree, parent=HEAD.
            new_commit = self._api_json(
                transport,
                create_commit_request(
                    repo,
                    message=(
                        f"ci: re-trigger dropped required workflows on "
                        f"#{pr_number} (OMN-13416)"
                    ),
                    tree_sha=tree_sha,
                    parent_sha=head_sha,
                ),
            )
            new_sha = new_commit.get("sha")
            if not isinstance(new_sha, str) or not new_sha:
                return False, "commit creation returned no sha"

            # 4. Fast-forward the branch ref to the new commit.
            self._api_json(
                transport, fast_forward_ref_request(repo, head_branch, new_sha)
            )
            return True, None
        except _GithubCallError as exc:
            return False, str(exc)
        except GithubLandingTransportError as exc:
            return False, str(exc)

    def _api_json(
        self,
        transport: UrllibGithubLandingTransport,
        request: ModelGithubHttpRequest,
    ) -> dict[str, object]:
        """Send one GitHub REST call and return its JSON body, or raise."""
        response = transport.send_sync(request)
        if not 200 <= response.status < 300:
            raise _GithubCallError(response.message())
        return dict(response.body or {})


class _GithubCallError(RuntimeError):
    """A GitHub call answered with a non-2xx status; the message is GitHub's."""
