# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-22956): the fault injection tests, run from the node directory the ticket's falsifier names.

CI collects only ``tests/`` (OMN-14338), so the tests live in ``tests/nodes/node_install_reading_compute/``;
this module re-exports them so ``pytest src/omnimarket/nodes/node_install_reading_compute`` runs the same ones.
"""

from tests.nodes.node_install_reading_compute.test_fault_injection import *  # noqa: F403
