# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's working memory is shared by every instance in the process, so isolate each test (OMN-26043)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnimarket.nodes.node_host_install_orchestrator.handlers.handler_host_install_orchestrator import (
    HandlerHostInstallOrchestrator,
)


@pytest.fixture(autouse=True)
def _isolate_shared_working_memory() -> Iterator[None]:
    HandlerHostInstallOrchestrator._contexts.clear()
    HandlerHostInstallOrchestrator._tree_install_orders.clear()
    yield
    HandlerHostInstallOrchestrator._contexts.clear()
    HandlerHostInstallOrchestrator._tree_install_orders.clear()
