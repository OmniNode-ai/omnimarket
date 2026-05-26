# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerObservabilitySinkEffect — Wave 4 stub [OMN-12217].

This node replaces the direct ActionLogger + Postgres writes made inline by the
observability skill.  All observability I/O routes through the dispatch bus;
the inline bus bypass in the skill is the architectural violation this node
corrects.

Implementation is deferred to Wave 5 (see contract.yaml metadata).
"""

from __future__ import annotations

from typing import Literal

from omnimarket.nodes.node_observability_sink_effect.models.model_observability_sink_input import (
    ModelObservabilitySinkInput,
)
from omnimarket.nodes.node_observability_sink_effect.models.model_observability_sink_output import (
    ModelObservabilitySinkOutput,
)


class HandlerObservabilitySinkEffect:
    """EFFECT: persist observability events to Kafka and/or PostgreSQL.

    Wave 4 contract-only stub — implementation deferred to Wave 5.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    async def handle(  # stub-ok
        self, request: ModelObservabilitySinkInput
    ) -> ModelObservabilitySinkOutput:
        raise NotImplementedError(  # stub-ok
            "HandlerObservabilitySinkEffect is a Wave 4 contract-only stub. "
            "Implementation is deferred to Wave 5 (OMN-12217)."
        )


__all__: list[str] = ["HandlerObservabilitySinkEffect"]
