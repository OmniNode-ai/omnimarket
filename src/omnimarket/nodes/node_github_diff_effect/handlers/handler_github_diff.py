# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_github_diff_effect (OMN-13210 / B1).

EFFECT node. Resolves a review target's content: a pull-request unified diff via
the GitHub REST API (``application/vnd.github.v3.diff`` media type), or a local
file's content. Replaces the legacy shelled ``gh pr diff`` in the hostile
reviewer workflow runner.

The GitHub token is resolved at ``handle()`` time from the contract-declared
``api_key_ref`` (``GITHUB_TOKEN``) via the canonical secret-store resolver — no
direct ``os.environ`` read and no subprocess shell-out.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.config.service_endpoints import GITHUB_REST_URL
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.model_review_target_snapshot import ModelReviewTargetSnapshot
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_github_diff_effect.models.model_github_diff import (
    ModelGithubDiffCommand,
    ModelGithubDiffResolvedEvent,
)

_HANDLER_ID = "node_github_diff_effect"
_GITHUB_API_VERSION = "2026-03-10"
_REQUEST_TIMEOUT = 30.0
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class HandlerGithubDiffEffect:
    """EFFECT: resolve a PR unified diff or a local file's content."""

    async def handle(self, request: ModelGithubDiffCommand) -> ModelHandlerOutput[None]:
        """Resolve the review-target content and emit it as a result event."""
        snapshot: ModelReviewTargetSnapshot
        if request.file_path is not None:
            content = await asyncio.to_thread(self._read_file, request.file_path)
            snapshot = ModelReviewTargetSnapshot.unavailable(
                "a file target has no pull request head or base"
            )
        else:
            github_ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
            github_secret = await resolve_api_key_async(github_ref)
            if github_secret is None:
                raise RuntimeError(
                    f"api_key_ref {github_ref!r} resolved to None — "
                    "ensure GITHUB_TOKEN is set in the secret store."
                )
            token = github_secret.get_secret_value()
            # repo/pr_number are guaranteed present by the command validator.
            content, snapshot = await asyncio.to_thread(
                self._read_pr_snapshot,
                str(request.repo),
                int(request.pr_number),  # type: ignore[arg-type]
                token,
            )

        event = ModelGithubDiffResolvedEvent(
            correlation_id=request.correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            file_path=request.file_path,
            content=content,
            content_chars=len(content),
            snapshot=snapshot,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            events=(event,),
        )

    @staticmethod
    def _read_file(file_path: str) -> str:
        path = Path(file_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"review file_path does not exist: {file_path}")
        if not path.is_file():
            raise IsADirectoryError(f"review file_path is not a file: {file_path}")
        content = path.read_text(encoding="utf-8")
        return f"File review target: {file_path}\n\n{content}"

    def _read_pr_snapshot(
        self, repo: str, pr_number: int, token: str
    ) -> tuple[str, ModelReviewTargetSnapshot]:
        """Read the diff bracketed by two head/base reads (OMN-21189).

        The snapshot is established only when both bracketing reads return the
        same head and base shas, so the diff text belongs to that head. A push
        between the reads, or a failed ref read, yields an unavailable snapshot
        with the reason; no sha is taken from either read in that case.
        """
        refs_before: tuple[str, str] | None = None
        unavailable_reason: str | None = None
        try:
            refs_before = self._fetch_pr_refs(repo, pr_number, token)
        except (RuntimeError, ValueError) as exc:
            unavailable_reason = f"head and base read before the diff failed: {exc}"

        diff = self._fetch_pr_diff(repo, pr_number, token)

        if refs_before is not None:
            try:
                refs_after = self._fetch_pr_refs(repo, pr_number, token)
            except (RuntimeError, ValueError) as exc:
                unavailable_reason = f"head and base read after the diff failed: {exc}"
            else:
                if refs_after != refs_before:
                    unavailable_reason = (
                        "pull request head or base moved between reads "
                        f"(head {refs_before[0]} -> {refs_after[0]}, "
                        f"base {refs_before[1]} -> {refs_after[1]})"
                    )
                else:
                    return diff, ModelReviewTargetSnapshot.established(
                        head_sha=refs_before[0],
                        base_sha=refs_before[1],
                        diff_text=diff,
                    )
        return diff, ModelReviewTargetSnapshot.unavailable(
            unavailable_reason or "head and base were not read"
        )

    def _fetch_pr_refs(self, repo: str, pr_number: int, token: str) -> tuple[str, str]:
        """Return ``(head_sha, base_sha)`` of a pull request as the API reports it now."""
        owner, _, repo_name = repo.partition("/")
        if not owner or not repo_name:
            raise ValueError(f"invalid repo slug: {repo!r}")
        req = urllib.request.Request(
            f"{GITHUB_REST_URL}/repos/{owner}/{repo_name}/pulls/{pr_number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"failed to read head and base for {repo}#{pr_number}: "
                f"{exc.code} {exc.reason}"
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"failed to read head and base for {repo}#{pr_number}: {exc}"
            ) from exc
        try:
            head_sha = str(body["head"]["sha"])
            base_sha = str(body["base"]["sha"])
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"pull request {repo}#{pr_number} response carries no head or base sha"
            ) from exc
        return head_sha, base_sha

    def _fetch_pr_diff(self, repo: str, pr_number: int, token: str) -> str:
        owner, _, repo_name = repo.partition("/")
        if not owner or not repo_name:
            raise ValueError(f"invalid repo slug: {repo!r}")

        req = urllib.request.Request(
            f"{GITHUB_REST_URL}/repos/{owner}/{repo_name}/pulls/{pr_number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3.diff",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                diff: str = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"failed to resolve PR diff for {repo}#{pr_number}: "
                f"{exc.code} {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(
                f"failed to resolve PR diff for {repo}#{pr_number}: {exc}"
            ) from exc

        if not diff.strip():
            raise ValueError(f"resolved empty PR diff for {repo}#{pr_number}")
        return diff


__all__: list[str] = ["HandlerGithubDiffEffect"]
