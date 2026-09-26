# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_focused_test_run_effect -- the focused test run, hosted on the lab
docker host and reached over its own bus command topic (OMN-19458).

The delegated write-run-repair loop publishes one focused-run request per run
and reads the receipt back from the terminal topic; the process on the lab
host (``onex test-loop serve-runs``) consumes the command topic and runs
``HandlerLabFocusedTestRunEffect``.
"""

from omnimarket.nodes.node_focused_test_run_effect.handlers.handler_lab_focused_test_run_effect import (
    DEFAULT_ALLOWED_OWNERS,
    HandlerLabFocusedTestRunEffect,
)
from omnimarket.nodes.node_focused_test_run_effect.protocols.local_shell_focused_run_subprocess import (
    LocalShellFocusedRunSubprocess,
)


class NodeFocusedTestRunEffect(HandlerLabFocusedTestRunEffect):
    """ONEX entry-point wrapper for HandlerLabFocusedTestRunEffect."""


__all__ = [
    "DEFAULT_ALLOWED_OWNERS",
    "HandlerLabFocusedTestRunEffect",
    "LocalShellFocusedRunSubprocess",
    "NodeFocusedTestRunEffect",
]
