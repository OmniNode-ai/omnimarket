# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill reducer's parity tests, collected from the default test surface.

``FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" --co -q``
reads ``tests/`` (the repository's ``testpaths``), so the node's parity tests, which live in
``src/omnimarket/nodes/node_floor_fill_reducer/tests``, are re-exported here under the same
``floor_fill_parity`` marker; the Redpanda leg keeps its own ``kafka`` marker.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_fill_reducer.tests import (
    test_floor_fill_reducer_parity as _node,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

globals().update(
    {
        name: value
        for name, value in vars(_node).items()
        if name.startswith("test_") and callable(value)
    }
)
