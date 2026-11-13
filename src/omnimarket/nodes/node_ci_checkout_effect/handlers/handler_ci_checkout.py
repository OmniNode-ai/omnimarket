# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: answer a bus checkout request from this host's git-served sources.

A request whose host is not this host is refused. For each source in order, the first
holding the sha serves it. On a miss, when the overlay syncs on miss, the host's own
clone sync runs once and every source is re-checked. Never a GitHub fetch from the job.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_git import (
    CommandCloneSync,
    SubprocessCheckoutGit,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    HOST_NAME_ENV,
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    EnumCiCheckoutStatus,
    ModelCiCheckoutOverlay,
    ModelCiCheckoutRequest,
    ModelCiCheckoutResult,
    ModelCiCheckoutSource,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout_deployment import (
    ModelCiCheckoutDeployment,
)
from omnimarket.nodes.node_ci_checkout_effect.protocols.protocol_ci_checkout import (
    ProtocolCheckoutGit,
    ProtocolCloneSync,
)

_HANDLER_ID = "node_ci_checkout_effect"


def load_deployment(path: Path | None = None) -> ModelCiCheckoutDeployment:
    """The deployed overlay document: the explicit file, else ONEX_CI_CHECKOUT_OVERLAY, else the roots."""
    import yaml

    explicit = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    return load_structured_overlay(
        OVERLAY_ID,
        ModelCiCheckoutDeployment,
        explicit=explicit,
        file_env=OVERLAY_FILE_ENV,
    )


def load_overlay(
    path: Path | None = None,
    *,
    host_name: str | None = None,
    home: Path | None = None,
) -> ModelCiCheckoutOverlay:
    """One host's overlay; the host is the argument, else the environment variable the contract names."""
    name = host_name or os.environ.get(HOST_NAME_ENV, "").strip()
    if not name:
        raise NodeOverlayError(
            f"no host to answer for: pass host_name or set {HOST_NAME_ENV}"
        )
    return load_deployment(path).resolve(name, home=str(home or Path.home()))


class HandlerCiCheckout:
    """EFFECT: serve a repo at a sha from this host's clones, syncing on a miss.

    Every dependency is optional so the runtime can construct the handler with no
    arguments: the overlay is read on first use and the git and clone-sync adapters
    are built from it. Tests inject all three.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        *,
        overlay: ModelCiCheckoutOverlay | None = None,
        git: ProtocolCheckoutGit | None = None,
        sync: ProtocolCloneSync | None = None,
    ) -> None:
        self._overlay = overlay
        self._git = git
        self._sync = sync

    @property
    def overlay(self) -> ModelCiCheckoutOverlay:
        if self._overlay is None:
            self._overlay = load_overlay()
        return self._overlay

    def _git_adapter(self) -> ProtocolCheckoutGit:
        if self._git is None:
            self._git = SubprocessCheckoutGit(self.overlay.git_timeout_seconds)
        return self._git

    def _sync_adapter(self) -> ProtocolCloneSync:
        if self._sync is None:
            self._sync = CommandCloneSync(
                self.overlay.sync_command, self.overlay.sync_timeout_seconds
            )
        return self._sync

    def _holding_source(self, repo_name: str, sha: str) -> ModelCiCheckoutSource | None:
        """The first overlay source, in order, whose clone holds ``sha``."""
        git = self._git_adapter()
        for source in self.overlay.sources:
            if git.has_commit(source.path_template.format(repo_name=repo_name), sha):
                return source
        return None

    def _result(
        self,
        request: ModelCiCheckoutRequest,
        status: EnumCiCheckoutStatus,
        *,
        source: ModelCiCheckoutSource | None = None,
        upstream_fetches: int = 0,
        detail: str | None = None,
    ) -> ModelCiCheckoutResult:
        repo_name = request.repo.split("/")[-1]
        return ModelCiCheckoutResult(
            correlation_id=request.correlation_id,
            host=self.overlay.host_name,
            status=status,
            repo=request.repo,
            sha=request.sha,
            source_name=source.name if source else None,
            serve_url=(
                source.serve_url_template.format(repo_name=repo_name)
                if source
                else None
            ),
            upstream_fetches=upstream_fetches,
            detail=detail,
        )

    def handle(self, request: ModelCiCheckoutRequest) -> ModelCiCheckoutResult:
        overlay = self.overlay
        if request.host != overlay.host_name:
            return self._result(
                request,
                EnumCiCheckoutStatus.REFUSED,
                detail="addressed to another host",
            )
        repo_name = request.repo.split("/")[-1]
        sources_tried = [source.name for source in overlay.sources]
        upstream_fetches = 0
        try:
            source = self._holding_source(repo_name, request.sha)
            if source is not None:
                return self._result(request, EnumCiCheckoutStatus.SERVED, source=source)
            if not overlay.sync_on_miss:
                return self._result(
                    request,
                    EnumCiCheckoutStatus.NOT_FOUND,
                    detail=f"sha {request.sha} not held by any of {sources_tried}",
                )
            upstream_fetches = 1
            if not self._sync_adapter().sync(request.repo, request.ref):
                return self._result(
                    request,
                    EnumCiCheckoutStatus.FAILED,
                    upstream_fetches=upstream_fetches,
                    detail="clone sync failed",
                )
            source = self._holding_source(repo_name, request.sha)
            if source is not None:
                return self._result(
                    request,
                    EnumCiCheckoutStatus.SYNCED_ON_MISS,
                    source=source,
                    upstream_fetches=upstream_fetches,
                )
            return self._result(
                request,
                EnumCiCheckoutStatus.NOT_FOUND,
                upstream_fetches=upstream_fetches,
                detail=(
                    f"sha {request.sha} not held by any of {sources_tried} after sync"
                ),
            )
        except Exception as exc:  # a runtime failure is a typed result, never a raise
            return self._result(
                request,
                EnumCiCheckoutStatus.FAILED,
                upstream_fetches=upstream_fetches,
                detail=type(exc).__name__,
            )
