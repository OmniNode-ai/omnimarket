# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local mirror of node_dispatch_worker's 7-value role enum (OMN-15163).

CLAUDE.md forbids one node importing another node's private handler or model
package (``omnimarket.nodes.node_dispatch_worker.models`` is exactly that), so
this is a deliberate, test-locked value mirror of
``omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command.EnumWorkerRole``
rather than a cross-node import. ``tests/nodes/node_report_validation_compute/
test_role_mapping.py`` asserts the two enums' string values stay identical, so
drift between the two node-local definitions is a CI failure, not a silent
runtime mismatch.
"""

from __future__ import annotations

from enum import StrEnum


class EnumDispatchWorkerRole(StrEnum):
    """The 7 node_dispatch_worker role-prompt bodies (source of truth: that node)."""

    watcher = "watcher"
    fixer = "fixer"
    designer = "designer"
    auditor = "auditor"
    synthesizer = "synthesizer"
    sweep = "sweep"
    ops = "ops"


__all__ = ["EnumDispatchWorkerRole"]
