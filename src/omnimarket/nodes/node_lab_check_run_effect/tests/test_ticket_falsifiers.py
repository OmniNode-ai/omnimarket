# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket's falsifier commands (OMN-22950) name this directory; the suites live in tests/nodes/node_lab_check_run_effect,
the only place CI collects them (OMN-14338). This module re-exposes them here, so the command as written on the ticket
runs the same tests and no test is written twice."""

from __future__ import annotations

from tests.nodes.node_lab_check_run_effect import (
    test_golden_chain_lab_check_run_effect as _golden_and_error,
)
from tests.nodes.node_lab_check_run_effect import test_join_j8_poster as _join

for _suite in (_golden_and_error, _join):
    globals().update({n: v for n, v in vars(_suite).items() if n.startswith("test_")})
