# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for work progress compute, bound to its handler by contract."""

from __future__ import annotations

from omnimarket.nodes.node_work_progress_compute.handlers.handler_work_progress import (
    HandlerWorkProgress,
)


class NodeWorkProgressCompute(HandlerWorkProgress):
    """Pure compute: phase entries and budgets in, ordered progress findings out."""


__all__ = ["NodeWorkProgressCompute"]
