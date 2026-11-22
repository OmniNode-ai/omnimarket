# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime dispatch entrypoint for the savings projection."""

import logging
from typing import Any

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_savings.handlers.handler_compaction_savings import (
    POST_COMPACT_HOOK_EVENT,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)

_HOOK_EVENT_TOPIC_SUFFIX = ".hook-event.v1"
logger = logging.getLogger(__name__)


class SavingsProjectionWriter(RuntimeProjectionWriter):
    """Apply the existing writer once for each runtime delivery."""

    runner_type = SavingsProjectionRunner

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Pass over a hook event the projection ignores before any database setup.

        OMN-23043. The hook-event topic carries every Claude Code hook event and
        the savings projection acts on PostCompact only; the runner already
        returns True for any other hook event with no read and no write. The base
        writer builds a runner and opens and attests the workload bindings for
        each delivery first, which held the consumer to about two events a second
        against a topic that grows faster, so its backlog never fell. The skip is
        the runner's own result for such an event, reached without that setup.

        The delivery context is read first, through the same typed request the
        base writer reads, so a delivery that lacks ``_partition`` or ``_offset``
        is refused here with ``ProjectionDeliveryContextMissingError`` as it is
        there instead of being passed over, and a skipped delivery is logged
        with the coordinates it was skipped at.
        """
        delivery = ModelProjectionRequest.from_injected(
            request
        ).require_delivery_context()
        topic = str(delivery.topic)
        if (
            topic.endswith(_HOOK_EVENT_TOPIC_SUFFIX)
            and request.get("hook_event_name") != POST_COMPACT_HOOK_EVENT
        ):
            logger.debug(
                "savings projection passed over an ignored hook event at %s[%d]@%d",
                topic,
                delivery.partition,
                delivery.offset,
            )
            return {"projected": True}
        return super().handle(request)
