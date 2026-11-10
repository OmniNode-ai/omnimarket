# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden and error chain tests of the stop drill, run from the node's own directory (OMN-22943 AC3).

The tests live under ``tests/nodes/node_process_spine_reading_compute``, the only place CI collects them from; this module
makes the ticket's falsifier command (``pytest src/omnimarket/nodes/node_process_spine_reading_compute -k chain``) run
the same functions.
"""

from __future__ import annotations

from importlib import import_module

_SOURCE = import_module(
    "tests.nodes.node_process_spine_reading_compute.test_golden_chain_process_spine_reading"
)
globals().update({k: v for k, v in vars(_SOURCE).items() if k.startswith("test_")})
