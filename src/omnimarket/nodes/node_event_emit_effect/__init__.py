"""Event emit effect node package (OMN-15965 R1).

Thin-publish-to-Kafka EFFECT node with a file-based spool outbox for
durability, replacing the Unix-socket ``node_emit_daemon`` transport for
new callers. ``node_emit_daemon`` itself is deleted separately under R5
(OMN-15974); this node must not import its Python modules.
"""

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)

# ModelEmitRequest is the handler's input, so it is part of this node's public
# surface: a caller outside the node builds one without reaching into the
# node's models package (OMN-20154, the provider quota observation sink).
__all__ = ["HandlerEventEmitEffect", "ModelEmitRequest"]
