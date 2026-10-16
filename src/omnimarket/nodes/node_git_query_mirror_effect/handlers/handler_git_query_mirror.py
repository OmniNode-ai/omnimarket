# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect handler for node_git_query_mirror_effect (OMN-19617).

Runs one operation (a fetch-only mirror sync, or one git read) against the
per-repo bare mirror and emits the typed result as the node's terminal event.
Git runs off the event loop. The only GitHub API call it can make is the
labelled fallback, when a targeted git fetch fails.

A sync records squash merges as pending before advancing its scan. The existing
event emit node's spool and Kafka adapter publish them to the contract topic;
only then does this handler mark them published. Each invocation retries pending
payloads from earlier processes. Delivery is at least once: a crash after broker
acceptance but before the local acknowledgement can repeat the same event ID.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_event_emit_effect import (
    KafkaEventPublisher,
    ProtocolPublishAdapter,
)
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
        publish_adapter: ProtocolPublishAdapter | None = None,
    ) -> None:
        self._published_merges = published_merges
        self._remote_url_for = remote_url_for
        self._gh_runner = gh_runner
        self._reference_clone_for = reference_clone_for
        self._publish_adapter = publish_adapter

    def mirror(self) -> GitQueryMirror:
        return GitQueryMirror(
            root=resolve_mirror_root(),
            remote_url_for=self._remote_url_for,
            gh_runner=self._gh_runner,
            reference_clone_for=self._reference_clone_for,
            published_merges=self._published_merges,
        )

    async def handle(self, request: ModelGitQueryRequest) -> ModelHandlerOutput[None]:
        """Run the query, publish pending merges, then emit the typed response."""
        mirror = self.mirror()
        # Replay before fetching: an upstream outage must not strand a durable
        # payload that needs no further git reads to publish.
        await self._publish_pending(mirror, request)
        result = await asyncio.to_thread(mirror.query, request)
        await self._publish_pending(mirror, request)
        response = ModelGitQueryResponse(
            correlation_id=request.correlation_id, results=(result,)
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            events=(response,),
        )

    async def _publish_pending(
        self, mirror: GitQueryMirror, request: ModelGitQueryRequest
    ) -> None:
        pending = await asyncio.to_thread(mirror.pending_merges, request.repo)
        if not pending:
            return
        adapter = self._publish_adapter
        if adapter is None:
            from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

            config = ModelKafkaEventBusConfig().apply_environment_overrides()
            adapter = KafkaEventPublisher(config.bootstrap_servers, source=_HANDLER_ID)
        for event in pending:
            await asyncio.to_thread(
                adapter.publish,
                event.topic,
                event.model_dump(mode="json"),
                key=event.repo,
                correlation_id=str(request.correlation_id),
                timeout_seconds=5.0,
            )
            await asyncio.to_thread(mirror.mark_merge_published, event)


__all__: list[str] = ["HandlerGitQueryMirrorEffect"]
