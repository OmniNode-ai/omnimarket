# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill lines compute's tests, collected from the default test surface.

The tests live beside the node (``src/omnimarket/nodes/node_floor_fill_lines_compute/tests``)
and the focused command runs them there. ``pytest tests/`` is what hosted CI runs, so this
module re-exports every test function of the node's test_floor_fill_lines.py; the module-level markers of the
original are the markers below.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_fill_lines_compute.tests import (
    test_floor_fill_lines as _node,
)

pytestmark = pytest.mark.unit

globals().update(
    {
        name: value
        for name, value in vars(_node).items()
        if name.startswith("test_") and callable(value)
    }
)
