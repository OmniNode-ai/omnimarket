# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The detectors of this node share one process-wide index; each test starts with it empty."""

from collections.abc import Iterator

import pytest

from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_ci_red import (
    PROCESS_INDEX,
)


@pytest.fixture(autouse=True)
def empty_process_index() -> Iterator[None]:
    PROCESS_INDEX.clear()
    yield
    PROCESS_INDEX.clear()
