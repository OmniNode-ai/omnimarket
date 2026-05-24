# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for codebase intelligence bridge operations.

Routes incoming query requests to the configured provider adapter
(default: AdapterRepoWiseCLI) and surfaces _meta fields from the response.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from omnimarket.nodes.node_codebase_intelligence_bridge_effect.adapters.adapter_repowise_cli import (
    AdapterRepoWiseCLI,
)
from omnimarket.nodes.node_codebase_intelligence_bridge_effect.models.model_codebase_intelligence_query_request import (
    ModelCodebaseIntelligenceQueryRequest,
)
from omnimarket.nodes.node_codebase_intelligence_bridge_effect.models.model_codebase_intelligence_query_response import (
    ModelCodebaseIntelligenceQueryResponse,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_codebase_intelligence_bridge_effect.adapters.protocol_codebase_intelligence import (
        ProtocolCodebaseIntelligence,
    )

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 10.0

__all__ = ["HandlerCodebaseIntelligenceBridge"]


def _extract_meta(raw: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Pull confidence, retrieval_quality, stale_warning out of _meta."""
    meta_raw = raw.get("_meta")
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    confidence: str | None = meta.get("confidence")
    retrieval_quality: str | None = meta.get("retrieval_quality")
    stale_warning: str | None = meta.get("stale_warning")
    return confidence, retrieval_quality, stale_warning


class HandlerCodebaseIntelligenceBridge:
    """Routes codebase intelligence queries to a provider adapter.

    The handler depends only on ProtocolCodebaseIntelligence — the default
    adapter is AdapterRepoWiseCLI but any conforming object may be injected.

    Parameters
    ----------
    adapter:
        Provider adapter. Defaults to ``AdapterRepoWiseCLI`` with the
        contract-configured timeout.
    timeout_seconds:
        Per-query timeout in seconds (mirrors contract config.timeout_seconds).
    """

    def __init__(
        self,
        *,
        adapter: ProtocolCodebaseIntelligence | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout = timeout_seconds
        self._adapter: ProtocolCodebaseIntelligence = adapter or AdapterRepoWiseCLI(
            timeout_seconds=timeout_seconds
        )

    async def handle(
        self,
        request: ModelCodebaseIntelligenceQueryRequest,
    ) -> ModelCodebaseIntelligenceQueryResponse:
        """Execute the requested codebase intelligence operation.

        Never raises — all errors are captured in the response model.
        """
        try:
            raw = await asyncio.wait_for(
                self._adapter.query(
                    operation=request.operation,
                    query=request.query,
                    targets=request.targets,
                    include=request.include,
                ),
                timeout=self._timeout,
            )
        except TimeoutError:
            logger.warning(
                "codebase_intelligence_bridge: timeout after %.1fs (op=%s)",
                self._timeout,
                request.operation,
            )
            return ModelCodebaseIntelligenceQueryResponse(
                operation=request.operation,
                status="timeout",
                error_message=f"Provider timed out after {self._timeout}s",
            )
        except Exception as exc:
            logger.exception(
                "codebase_intelligence_bridge: error (op=%s): %s",
                request.operation,
                exc,
            )
            return ModelCodebaseIntelligenceQueryResponse(
                operation=request.operation,
                status="error",
                error_message=str(exc),
            )

        confidence, retrieval_quality, stale_warning = _extract_meta(raw)

        return ModelCodebaseIntelligenceQueryResponse(
            operation=request.operation,
            status="success",
            result=raw,
            confidence=confidence,
            retrieval_quality=retrieval_quality,
            stale_warning=stale_warning,
        )
