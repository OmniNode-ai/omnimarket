# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GLM usage record compute: one harness run on the Coding Plan becomes a usage event and, on a cap refusal, a refusal event.

``HandlerGlmUsageRecord.handle(ModelGlmUsageRecordRequest) -> ModelGlmUsageRecordResult`` (definition-B).
"""

from omnimarket.nodes.node_glm_usage_record_compute.handlers.handler_glm_usage_record import (
    HandlerGlmUsageRecord,
)


class NodeGlmUsageRecordCompute(HandlerGlmUsageRecord):
    """ONEX entry-point wrapper for HandlerGlmUsageRecord."""


__all__ = ["HandlerGlmUsageRecord", "NodeGlmUsageRecordCompute"]
