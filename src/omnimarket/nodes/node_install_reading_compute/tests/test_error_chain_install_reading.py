# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-22956): the error chain tests, run from the node directory the ticket's falsifier names.

CI collects only ``tests/`` (OMN-14338), so the tests live in ``tests/nodes/node_install_reading_compute/``;
this module re-exports them so ``pytest src/omnimarket/nodes/node_install_reading_compute`` runs the same ones.
"""

from tests.nodes.node_install_reading_compute.test_error_chain_install_reading import *  # noqa: F403
