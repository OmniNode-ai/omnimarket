# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelProjectionRequest: one delivery to an in-process projection runner (OMN-21536)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.projection.handler_shim import (
    INJECTED_OFFSET_KEY,
    INJECTED_PARTITION_KEY,
    INJECTED_TOPIC_KEY,
)

# The runtime-injected key that carries a delivery's dedup identity, beside the topic,
# partition and offset. handler_shim does not name it because the producer does not
# inject it: a caller with no coordinates (a scheduled refresh, a test) supplies its own.
FALLBACK_ID_KEY = "_fallback_id"

_DELIVERY_KEYS = frozenset(
    {INJECTED_TOPIC_KEY, INJECTED_PARTITION_KEY, INJECTED_OFFSET_KEY, FALLBACK_ID_KEY}
)


class ModelProjectionRequest(BaseModel):
    """One delivery to an in-process projection runner, split from the injected dict.

    The shared runtime hands a projection writer one ``dict`` per delivery: the event
    payload beside the keys it injects (``_topic``, ``_partition``, ``_offset``,
    ``_fallback_id`` and the kernel seam's ``_db``, ``_event_type``, ``_envelope_id``).
    Every runner used to pop those keys by hand at the top of its own ``handle``;
    ``BaseProjectionRunner.handle`` now does it once and passes the runner this request
    through ``project_request``.

    ``payload`` is the dict with the four delivery keys removed. The kernel seam's other
    keys stay in it, exactly as the runners' own pops left them, because each runner
    already strips the ones its event model forbids.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str | None = Field(
        default=None, description="Source topic, or None when the runtime named none."
    )
    partition: int = Field(default=0, description="Source partition, 0 when absent.")
    offset: int = Field(default=0, description="Source offset, 0 when absent.")
    has_coordinates: bool = Field(
        default=False,
        description="True when the delivery carried both a partition and an offset.",
    )
    fallback_id: str = Field(
        default="", description="Caller-supplied dedup identity, empty when absent."
    )
    payload: dict[str, object] = Field(
        description="The delivery with the four delivery keys removed."
    )

    @classmethod
    def from_injected(cls, input_data: Mapping[str, object]) -> Self:
        """Split the dict the projection arm hands ``handle`` into a typed request.

        Reads the source coordinates OMN-18905 requires every writer to carry onto the
        snapshot deltas it publishes. Non-destructive: ``input_data`` is not mutated.
        """
        return cls.model_validate(
            {
                "topic": (
                    str(input_data[INJECTED_TOPIC_KEY])
                    if INJECTED_TOPIC_KEY in input_data
                    else None
                ),
                "partition": input_data.get(INJECTED_PARTITION_KEY, 0),
                "offset": input_data.get(INJECTED_OFFSET_KEY, 0),
                "has_coordinates": (
                    INJECTED_PARTITION_KEY in input_data
                    and INJECTED_OFFSET_KEY in input_data
                ),
                "fallback_id": str(input_data.get(FALLBACK_ID_KEY, "")),
                "payload": {
                    key: value
                    for key, value in input_data.items()
                    if key not in _DELIVERY_KEYS
                },
            }
        )

    def topic_or(self, default: str) -> str:
        """The delivery's topic, else ``default`` when the runtime named none."""
        return default if self.topic is None else self.topic

    def message_meta(self, topic: str = "") -> MessageMeta:
        """The dedup coordinates a runner's ``project_event`` takes."""
        # Deferred: runner.py imports this model for ``BaseProjectionRunner.handle``.
        from omnimarket.projection.runner import MessageMeta

        return MessageMeta(
            partition=self.partition,
            offset=self.offset,
            fallback_id=self.fallback_id,
            topic=topic,
        )
