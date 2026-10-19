# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ProtocolContextTierSource: the read the orchestrator makes of each tier's source.

The orchestrator needs one call of a source: ``handle(ModelContextSourceRequest) ->
ModelContextSourceResult``, over the omnibase_core wire models. The protocol is
structural, so every source that satisfies the SPI's ``ProtocolContextSource`` also
satisfies this one, and the node does not depend on the SPI release that adds it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)


@runtime_checkable
class ProtocolContextTierSource(Protocol):
    """A source that answers one context-source read per call."""

    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        """Answer one read at the requested revision."""
        ...
