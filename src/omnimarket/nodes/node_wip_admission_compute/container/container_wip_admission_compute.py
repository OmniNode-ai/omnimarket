# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_wip_admission_compute."""

from __future__ import annotations

from omnimarket.nodes.node_wip_admission_compute.handlers.handler_wip_admission import (
    HandlerWipAdmission,
)


def create_handler_wip_admission() -> HandlerWipAdmission:
    """Bind the WIP admission handler."""
    return HandlerWipAdmission()


__all__ = ["create_handler_wip_admission"]
