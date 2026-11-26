# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C30 and C31 handlers pass the handler compliance sweep's topic and transport checks (OMN-19966).

The sweep refuses a topic string written in a handler module; the topics the probes read are declared in each node's
contract and its model module, and the handlers import them (the way the sibling C32 probe does).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    NodeComplianceSweep,
)

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
PROBES = (
    "node_c30_claude_code_plugin_probe_effect",
    "node_c31_local_dashboard_parity_probe_effect",
)


@pytest.mark.parametrize("node", PROBES)
def test_handlers_carry_no_hardcoded_topic_and_no_undeclared_transport(
    node: str,
) -> None:
    sweep = NodeComplianceSweep()
    found = []
    for handler in sorted((NODES / node / "handlers").glob("*.py")):
        lines = handler.read_text(encoding="utf-8").splitlines()
        found += sweep._check_hardcoded_topics("", str(handler), node, lines)
        found += sweep._check_transport_imports("", str(handler), node, handler)
    assert [(v.violation_type, v.message, v.line) for v in found] == []
