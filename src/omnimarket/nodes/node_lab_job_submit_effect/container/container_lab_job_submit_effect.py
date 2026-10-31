# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.nodes.node_lab_job_submit_effect import (
    HandlerLabJobSubmitEffect,
)


def build_handler_lab_job_submit_effect(
    bus: ProtocolLabRunBus,
) -> HandlerLabJobSubmitEffect:
    """Build the submission handler with its opened bus."""
    return HandlerLabJobSubmitEffect(bus)
