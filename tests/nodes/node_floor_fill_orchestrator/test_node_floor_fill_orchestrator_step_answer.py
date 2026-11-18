# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill's typed step answer scenarios, collected from the default test surface (see the entry module)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.tests import (
    test_floor_fill_step_answer as _node,
)

pytestmark = [pytest.mark.unit]

globals().update(
    {
        name: value
        for name, value in vars(_node).items()
        if name.startswith("test_") and callable(value)
    }
)
