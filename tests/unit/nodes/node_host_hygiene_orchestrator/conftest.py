# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host hygiene test starts with no open run and no cleanup remembered as run."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.pending_runs import (
    PENDING_HOST_HYGIENE_RUNS,
)


@pytest.fixture(autouse=True)
def _fresh_pending_runs() -> Iterator[None]:
    PENDING_HOST_HYGIENE_RUNS.clear()
    yield
    PENDING_HOST_HYGIENE_RUNS.clear()
