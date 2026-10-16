"""Event emit effect node package (OMN-15965 R1).

Thin-publish-to-Kafka EFFECT node with a file-based spool outbox for
durability, replacing the Unix-socket ``node_emit_daemon`` transport for
new callers. ``node_emit_daemon`` itself is deleted separately under R5
(OMN-15974); this node must not import its Python modules.
"""

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
    KafkaEventPublisher,
    ProtocolPublishAdapter,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import (
    SpoolFile,
    SpoolOutbox,
    SpoolRecord,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    EnumDurabilityTier,
)

# ModelEmitRequest is the handler's input, so it is part of this node's public
# surface: a caller outside the node builds one without reaching into the
# node's models package (OMN-20154, the provider quota observation sink).
# Public durability primitives let other effect nodes reuse the spool and
# acknowledged Kafka adapter without importing this node's private packages.
__all__ = [
    "EnumDurabilityTier",
    "HandlerEventEmitEffect",
    "KafkaEventPublisher",
    "ModelEmitRequest",
    "ProtocolPublishAdapter",
    "SpoolFile",
    "SpoolOutbox",
    "SpoolRecord",
]
