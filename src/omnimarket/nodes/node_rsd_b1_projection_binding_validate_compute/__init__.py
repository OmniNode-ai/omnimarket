"""Offline B1 projection-binding validation node."""

from .handlers.handler_b1_projection_binding import (
    HandlerB1ProjectionBinding,
    validate_b1_projection_binding,
)


class NodeRsdB1ProjectionBindingValidateCompute(HandlerB1ProjectionBinding):
    """ONEX wrapper for the effect-free B1 verifier."""


__all__ = [
    "HandlerB1ProjectionBinding",
    "NodeRsdB1ProjectionBindingValidateCompute",
    "validate_b1_projection_binding",
]
