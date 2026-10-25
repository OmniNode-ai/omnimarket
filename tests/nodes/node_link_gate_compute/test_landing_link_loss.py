# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""From plugin test_landing_link_loss.py; tests of plugin scripts stay with those scripts."""

import pytest

from omnimarket.nodes.node_link_gate_compute import node as link_node

pytestmark = pytest.mark.unit


def test_the_node_judges_one_request_and_never_the_launching_host_or_a_bus_unit() -> (
    None
):
    judge = link_node.NodeLinkGateCompute().handle
    request = link_node.ModelLinkGateRequest
    assert judge(request(loss_pct=40.0, prior=(40.0,))).refusal
    assert judge(request(loss_pct=40.0, prior=(40.0,))).degraded
    assert judge(request(loss_pct=40.0)).refusal is None
    assert judge(request(loss_pct=40.0)).degraded
    assert judge(
        request(loss_pct=None, prior=(40.0,))
    ) == link_node.ModelLinkGateVerdict(refusal=None, degraded=False)
    assert judge(request(loss_pct=90.0, prior=(90.0,), local=True)).refusal is None
    assert judge(
        request(loss_pct=90.0, prior=(90.0,), bus=True)
    ) == link_node.ModelLinkGateVerdict(refusal=None, degraded=False)
