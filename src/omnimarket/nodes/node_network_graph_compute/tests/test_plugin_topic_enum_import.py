# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-23141 AC1 tests, collected from this node's own path.

The scan reads source files, which the node's purity checks forbid inside a node package, so the
tests live under tests/unit/nodes/node_network_graph_compute and are re-exported here; the
ticket's falsifier command runs pytest over this directory.
"""

from __future__ import annotations

import pytest

from tests.unit.nodes.node_network_graph_compute.test_plugin_topic_enum_import import (
    test_plugin_topic_enum_import_market_source_holds_none,
    test_plugin_topic_enum_import_scan_finds_each_import_form,
)

pytestmark = pytest.mark.unit

__all__ = [
    "test_plugin_topic_enum_import_market_source_holds_none",
    "test_plugin_topic_enum_import_scan_finds_each_import_form",
]
