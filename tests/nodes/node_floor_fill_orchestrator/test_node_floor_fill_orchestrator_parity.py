# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill orchestrator's parity run, collected from the default test surface.

The tests live beside the node (``src/omnimarket/nodes/node_floor_fill_orchestrator/tests``) and the focused command
runs them there. ``pytest tests/`` is what hosted CI runs, so this module re-exports every test function of the
node's ``test_floor_fill_orchestrator_parity.py``; the module-level markers of the original are the markers below.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.tests import (
    test_floor_fill_orchestrator_parity as _node,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

globals().update(
    {
        name: value
        for name, value in vars(_node).items()
        if name.startswith("test_") and callable(value)
    }
)
