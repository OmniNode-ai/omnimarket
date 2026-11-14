# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the sources through the container and call the pure merged-change reading node."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from omnimarket.models.model_merged_change_reading import ModelMcrRequest
from omnimarket.nodes.node_merged_change_reading_compute.handlers.handler_merged_change_reading import (
    HandlerMergedChangeReading,
)
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrRunRequest,
    ModelMcrRunResult,
)
from omnimarket.protocols.protocol_merged_change_sources import (
    ProtocolMergedChangeSources,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

    from omnimarket.models.model_merged_change_reading import ModelMcrConfig


class HandlerMergedChangeReadingEffect:
    """Boundary of the reading: every read happens in the sources the container resolves, none in the compute node."""

    def __init__(
        self,
        container: ModelONEXContainer,
        default_config: ModelMcrConfig | None = None,
    ) -> None:
        self._container = container
        self._default_config = default_config

    def _sources(self) -> ProtocolMergedChangeSources:
        key: Any = ProtocolMergedChangeSources
        sources: ProtocolMergedChangeSources = self._container.get_service(key)
        return sources

    def handle(self, request: ModelMcrRunRequest) -> ModelMcrRunResult:
        config = request.config or self._default_config
        if config is None:
            raise ValueError("no config: pass one or bind the contract's own block")
        sources = self._sources()
        changes = sources.resolve_changes(request.changes)
        runners = sources.read_runners()
        facts, build_unread, started = sources.read_build(changes)
        reading = HandlerMergedChangeReading().handle(
            ModelMcrRequest(
                now=request.now or datetime.now(UTC),
                config=config,
                build_started_at=started,
                changes=changes,
                runners=runners,
                build_facts=facts,
                build_unread=build_unread,
                runtime=sources.read_runtime(runners),
            )
        )
        return ModelMcrRunResult(
            reading=reading,
            runners_read=len(runners),
            changes_without_sha=sum(1 for c in changes if not c.merge_sha),
        )
