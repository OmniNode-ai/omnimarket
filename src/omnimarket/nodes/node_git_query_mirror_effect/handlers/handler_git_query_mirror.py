# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect handler for node_git_query_mirror_effect (OMN-19617).

Runs one operation (a fetch-only mirror sync, or one git read) against the
per-repo bare mirror and emits the typed result as the node's terminal event.
Git runs off the event loop. The only GitHub API call it can make is the
labelled fallback, when a targeted git fetch fails.

A sync also reports the squash merges that landed on the default branch since
the previous sync (OMN-19834). The handler returns one
``onex.evt.github.pr-merged.v1`` event per merge after the response, with
source ``mirror``, for the runtime to publish on the contract's
``event_bus.publish_topics``. Detection is git-only; it is how merges of
repositories with no Actions merge publisher reach the bus.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_git_query_mirror_effect.git_mirror import (
    GhRunner,
    GitQueryMirror,
    PublishedMerges,
    canonical_clone_for,
    default_gh_runner,
    default_remote_url,
    resolve_mirror_root,
)
from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    ModelGitQueryRequest,
    ModelGitQueryResponse,
    ModelMirrorPrMerged,
)

_HANDLER_ID = "node_git_query_mirror_effect"


class HandlerGitQueryMirrorEffect:
    """EFFECT: answer one PR read from a fetch-only git mirror, typed result out."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        remote_url_for: Callable[[str], str] = default_remote_url,
        gh_runner: GhRunner = default_gh_runner,
        reference_clone_for: Callable[[str], Path | None] = canonical_clone_for,
        published_merges: PublishedMerges | None = None,
    ) -> None:
        self._published_merges = published_merges
        self._remote_url_for = remote_url_for
        self._gh_runner = gh_runner
        self._reference_clone_for = reference_clone_for

    def mirror(self) -> GitQueryMirror:
        return GitQueryMirror(
            root=resolve_mirror_root(),
            remote_url_for=self._remote_url_for,
            gh_runner=self._gh_runner,
            reference_clone_for=self._reference_clone_for,
            published_merges=self._published_merges,
        )

    async def handle(self, request: ModelGitQueryRequest) -> ModelHandlerOutput[None]:
        """Run the operation and emit the typed response, then one event per merge found."""
        result = await asyncio.to_thread(self.mirror().query, request)
        merged: tuple[ModelMirrorPrMerged, ...] = tuple(
            event for r in (result,) for event in (r.merged_prs or [])
        )
        response = ModelGitQueryResponse(
            correlation_id=request.correlation_id, results=(result,)
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            events=(response, *merged),
        )


__all__: list[str] = ["HandlerGitQueryMirrorEffect"]
