# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the count's sources through the container and call the pure compute node."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from omnimarket.nodes.node_merged_running_count_compute.handlers.handler_merged_running_count import (
    HandlerMergedRunningCount,
)
from omnimarket.nodes.node_merged_running_count_compute.models.model_merged_running_count import (
    ModelMergedRunningCountRequest,
)
from omnimarket.nodes.node_merged_running_count_effect.models.model_merged_running_count_run import (
    ModelMergedRunningCountRunRequest,
    ModelMergedRunningCountRunResult,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_contract_config import (
    load_contract_config,
)
from omnimarket.protocols.protocol_merged_running_sources import (
    ProtocolMergedRunningSources,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer


class HandlerMergedRunningCountEffect:
    """Boundary of the count: every read happens in the sources the container resolves, none in the compute node."""

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    def _sources(self) -> ProtocolMergedRunningSources:
        key: Any = ProtocolMergedRunningSources
        sources: ProtocolMergedRunningSources = self._container.get_service(key)
        return sources

    def handle(
        self, request: ModelMergedRunningCountRunRequest
    ) -> ModelMergedRunningCountRunResult:
        sources = self._sources()
        prs = sources.read_prs()
        evidence = sources.read_evidence()
        count = HandlerMergedRunningCount().handle(
            ModelMergedRunningCountRequest(
                now=request.now or datetime.now(UTC),
                config=request.config or load_contract_config(),
                prs=prs,
                evidence=evidence,
                hosts=sources.read_hosts(),
                live_lanes=sources.read_live_lanes(),
            )
        )
        return ModelMergedRunningCountRunResult(
            count=count,
            pr_records_read=len(prs),
            evidence_records_read=len(evidence),
        )
