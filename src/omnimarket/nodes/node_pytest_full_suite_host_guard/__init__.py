# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pytest_full_suite_host_guard: may a full suite run on this host for this login (OMN-23367)."""

from omnimarket.nodes.node_pytest_full_suite_host_guard.handlers.handler_pytest_full_suite_host_guard import (
    HandlerPytestFullSuiteHostGuard,
)


class NodePytestFullSuiteHostGuard(HandlerPytestFullSuiteHostGuard):
    """ONEX entry-point wrapper for HandlerPytestFullSuiteHostGuard."""


__all__ = ["HandlerPytestFullSuiteHostGuard", "NodePytestFullSuiteHostGuard"]
