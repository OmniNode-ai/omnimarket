# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill reducer's own tests, collected from the default test surface.

The tests live beside the node (``src/omnimarket/nodes/node_floor_fill_reducer/tests``) and
the focused command runs them there. ``pytest tests/`` is what hosted CI runs, so this module
re-exports every test function of the node's conformance, invariant and rule tests; the
module-level marker of the original is the marker below.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_fill_reducer.tests import (
    test_floor_fill_reducer as _node,
)

pytestmark = pytest.mark.unit

globals().update(
    {
        name: value
        for name, value in vars(_node).items()
        if name.startswith("test_") and callable(value)
    }
)
