# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Convert a headless agent run's log into an OpenTelemetry GenAI trace."""

from .handlers.handler_trace_convert import HandlerTraceConvert


class NodeAgentTraceOtelCompute(HandlerTraceConvert):
    """Registered ONEX node."""
