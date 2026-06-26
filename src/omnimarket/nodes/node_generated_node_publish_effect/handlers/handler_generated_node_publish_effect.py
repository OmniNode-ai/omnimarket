# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerGeneratedNodePublishEffect -- publish a generated node package via PR.

SEA canonicalization Phase 0.2 (OMN-13606). This is the auto-PR / publish step
of the SEA self-extension loop and the second half of the generation spine: the
Phase 0.1 generation consumer hot-loads a generated node and scaffolds its full
canonical package into a staging directory; this EFFECT node takes that staged
package and opens a real PR for it.

Flow (all git/gh I/O via the injected ``run_fn`` -- no real subprocess in tests):

    1. Resolve the target repo's canonical clone root (fail-fast from OMNI_HOME).
    2. Create a fresh git worktree + branch off the base branch.
    3. Copy the staged canonical package into the repo node subdir.
    4. ``git add`` + ``git commit`` the package.
    5. ``git push`` the branch.
    6. ``gh pr create`` with a title + body carrying the OMN ticket + dod_evidence.
    7. Emit the resulting PR URL on the contract-declared publish topic.

Topics are read from contract.yaml (never hardcoded in the handler). Endpoints
are not used -- gh resolves the GitHub host from ambient auth; there are no
env-encoded endpoints in this node.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml

from omnimarket.nodes.node_generated_node_publish_effect.models.model_generated_node_publish_input import (
    ModelGeneratedNodePublishInput,
)
from omnimarket.nodes.node_generated_node_publish_effect.models.model_generated_node_publish_result import (
    ModelGeneratedNodePublishResult,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Subprocess runner signature: (argv) -> (returncode, stdout, stderr).
RunFn = Callable[[list[str]], tuple[int, str, str]]
# Resolver: repo slug (org/repo) -> canonical clone root Path.
RepoRootResolver = Callable[[str], Path]
# Sync (topic, bytes) -> None publisher injected by the runtime's Kafka adapter.
EventPublisher = Callable[[str, bytes], None]


def _default_run(cmd: list[str]) -> tuple[int, str, str]:
    """Default subprocess runner -- uses ambient GH_TOKEN / GITHUB_TOKEN."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def _default_repo_root(repo: str) -> Path:
    """Resolve the canonical clone root for ``org/repo`` under ``OMNI_HOME``.

    Fails fast (KeyError) when ``OMNI_HOME`` is unset -- no silent default
    (CLAUDE.md Rule 8). The clone directory is the repo's short name (the path
    segment after ``org/``), matching the canonical registry layout
    (``$OMNI_HOME/<repo>``).
    """
    omni_home = Path(os.environ["OMNI_HOME"])
    short_name = repo.split("/")[-1]
    return omni_home / short_name


def _load_publish_topic(contract_path: Path | None = None) -> str:
    """Read the single contract-declared publish topic.

    Topics are NEVER hardcoded in the handler -- they are resolved from
    contract.yaml so the wiring stays contract-driven (repo rule: keep event
    topics in contract.yaml, no hardcoded topic strings in handlers).
    """
    p = contract_path or _CONTRACT_PATH
    with open(p) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    event_bus: dict[str, Any] = data.get("event_bus", {})
    publish_topics: list[str] = list(event_bus.get("publish_topics", []))
    if not publish_topics:
        raise ValueError(
            "contract.yaml event_bus.publish_topics must declare the "
            "generated-node-published terminal topic; it is contract-driven"
        )
    return publish_topics[0]


class HandlerGeneratedNodePublishEffect:
    """Publishes a staged generated node package as a GitHub PR.

    Dependencies are injected via the constructor for testability: ``run_fn``
    captures git/gh subprocess calls, ``repo_root_resolver`` resolves the target
    clone root, ``event_publisher`` captures the terminal bus emit.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        run_fn: RunFn | None = None,
        repo_root_resolver: RepoRootResolver | None = None,
        event_publisher: EventPublisher | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._run: RunFn = run_fn or _default_run
        self._repo_root: RepoRootResolver = repo_root_resolver or _default_repo_root
        self._event_publisher: EventPublisher | None = event_publisher
        self._terminal_topic = _load_publish_topic(contract_path)

    @property
    def terminal_topic(self) -> str:
        """Terminal result topic this effect emits (contract-resolved)."""
        return self._terminal_topic

    async def handle(
        self, payload: ModelGeneratedNodePublishInput
    ) -> ModelGeneratedNodePublishResult:
        """Publish the staged package as a PR and emit the PR URL.

        Accepts a single typed ``ModelGeneratedNodePublishInput`` so the
        RuntimeLocal event-driven path resolves the initial-payload model from
        this handler's ``handle`` parameter annotation and dispatches headless.
        """
        correlation_id: UUID = payload.correlation_id
        node_name: str = payload.node_name
        repo: str = payload.repo

        logger.info(
            "[generated-publish] start (correlation_id=%s, node=%s, repo=%s)",
            correlation_id,
            node_name,
            repo,
        )

        staging = Path(payload.staging_dir)
        if not staging.is_dir():
            return self._terminal(
                payload,
                published=False,
                blocked_reason=f"staging dir does not exist: {staging}",
            )

        repo_root = self._repo_root(repo)
        branch = f"generated/{node_name}-{correlation_id}"

        with tempfile.TemporaryDirectory(prefix="onex-generated-publish-") as tmp:
            worktree_dir = Path(tmp) / node_name

            blocked = self._create_worktree(repo_root, worktree_dir, branch, payload)
            if blocked is not None:
                return self._terminal(payload, published=False, blocked_reason=blocked)

            dest = worktree_dir / payload.node_subdir / node_name
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(staging, dest, dirs_exist_ok=True)
            except OSError as exc:
                return self._terminal(
                    payload,
                    published=False,
                    blocked_reason=f"failed to copy staged package: {exc}",
                )

            blocked = self._commit_and_push(worktree_dir, branch, payload)
            if blocked is not None:
                return self._terminal(payload, published=False, blocked_reason=blocked)

            pr_url, blocked = self._open_pr(worktree_dir, branch, payload)
            if blocked is not None:
                return self._terminal(payload, published=False, blocked_reason=blocked)

        logger.info("[generated-publish] opened PR for %s: %s", node_name, pr_url)
        return self._terminal(
            payload,
            published=True,
            pr_url=pr_url,
            branch=branch,
        )

    def _create_worktree(
        self,
        repo_root: Path,
        worktree_dir: Path,
        branch: str,
        payload: ModelGeneratedNodePublishInput,
    ) -> str | None:
        """Create the worktree branch. Returns a blocked_reason or None on success."""
        cmd = [
            "git",
            "-C",
            str(repo_root),
            "worktree",
            "add",
            str(worktree_dir),
            "-b",
            branch,
            payload.base_branch,
        ]
        rc, _out, err = self._run(cmd)
        if rc != 0:
            return f"git worktree add failed (exit {rc}): {err.strip()}"
        return None

    def _commit_and_push(
        self,
        worktree_dir: Path,
        branch: str,
        payload: ModelGeneratedNodePublishInput,
    ) -> str | None:
        """Stage, commit, and push the package. Returns blocked_reason or None."""
        add_cmd = ["git", "-C", str(worktree_dir), "add", "--all"]
        rc, _out, err = self._run(add_cmd)
        if rc != 0:
            return f"git add failed (exit {rc}): {err.strip()}"

        message = (
            f"feat({payload.ticket}): publish generated node "
            f"{payload.node_name} (SEA self-extension)"
        )
        commit_cmd = ["git", "-C", str(worktree_dir), "commit", "-m", message]
        rc, _out, err = self._run(commit_cmd)
        if rc != 0:
            return f"git commit failed (exit {rc}): {err.strip()}"

        push_cmd = [
            "git",
            "-C",
            str(worktree_dir),
            "push",
            "--set-upstream",
            "origin",
            branch,
        ]
        rc, _out, err = self._run(push_cmd)
        if rc != 0:
            return f"git push failed (exit {rc}): {err.strip()}"
        return None

    def _open_pr(
        self,
        worktree_dir: Path,
        branch: str,
        payload: ModelGeneratedNodePublishInput,
    ) -> tuple[str | None, str | None]:
        """Open the PR via gh. Returns (pr_url, blocked_reason)."""
        title = (
            f"feat({payload.ticket}): publish generated node "
            f"{payload.node_name} (SEA self-extension)"
        )
        body = self._build_pr_body(payload, branch)
        cmd = [
            "gh",
            "pr",
            "create",
            "--repo",
            payload.repo,
            "--base",
            payload.base_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ]
        rc, out, err = self._run(cmd)
        if rc != 0:
            return None, f"gh pr create failed (exit {rc}): {err.strip()}"
        pr_url = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not pr_url:
            return None, "gh pr create returned no PR URL"
        return pr_url, None

    @staticmethod
    def _build_pr_body(payload: ModelGeneratedNodePublishInput, branch: str) -> str:
        """Build a PR body carrying the OMN ticket reference + dod_evidence."""
        return (
            f"Generated node `{payload.node_name}` published by the SEA "
            "self-extension loop (Phase 0.2 publish effect).\n\n"
            f"Ticket: {payload.ticket}\n"
            f"Branch: {branch}\n\n"
            "## dod_evidence\n\n"
            f"{payload.dod_evidence}\n"
        )

    def _terminal(
        self,
        payload: ModelGeneratedNodePublishInput,
        *,
        published: bool,
        pr_url: str | None = None,
        branch: str | None = None,
        blocked_reason: str | None = None,
    ) -> ModelGeneratedNodePublishResult:
        """Build the typed result and emit it on the contract-declared topic."""
        result = ModelGeneratedNodePublishResult(
            correlation_id=payload.correlation_id,
            node_name=payload.node_name,
            repo=payload.repo,
            published=published,
            pr_url=pr_url,
            branch=branch,
            blocked_reason=blocked_reason,
        )
        if self._event_publisher is not None:
            try:
                self._event_publisher(
                    self._terminal_topic,
                    result.model_dump_json().encode("utf-8"),
                )
            except Exception as exc:
                logger.warning(
                    "[generated-publish] emit terminal to %s failed: %s",
                    self._terminal_topic,
                    exc,
                )
        return result


__all__: list[str] = ["HandlerGeneratedNodePublishEffect"]
