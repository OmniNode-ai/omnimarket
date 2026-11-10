# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-23138 relation count tests, collected from this node's own path.

They walk the source tree and read files, which the node's purity checks forbid inside a node
package, so they live under tests/unit/nodes/node_network_graph_compute and are re-exported here;
the ticket's falsifier command runs pytest over this directory.
"""

from __future__ import annotations

import pytest

from tests.unit.nodes.node_network_graph_compute.test_relation_count_dispatch_tickets_plans import (
    test_relation_count_dispatch_tickets_plans_control_reads_one_for_a_planted_import,
    test_relation_count_dispatch_tickets_plans_reads_zero_in_this_tree,
    test_relation_count_dispatch_tickets_plans_the_create_module_is_on_the_dispatch_side,
)

pytestmark = pytest.mark.unit

__all__ = [
    "test_relation_count_dispatch_tickets_plans_control_reads_one_for_a_planted_import",
    "test_relation_count_dispatch_tickets_plans_reads_zero_in_this_tree",
    "test_relation_count_dispatch_tickets_plans_the_create_module_is_on_the_dispatch_side",
]
