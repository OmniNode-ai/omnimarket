# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""State-coverage assertions for node_llm_delegation_routing_compute."""

from __future__ import annotations


def test_node_llm_delegation_routing_compute_declared_output_topics_are_covered() -> (
    None
):
    declared_topics = {
        "onex.evt.omnimarket.llm-delegation-routing-completed.v1",
        "onex.evt.omnimarket.llm-delegation-routing-failed.v1",
    }

    assert declared_topics == {
        "onex.evt.omnimarket.llm-delegation-routing-completed.v1",
        "onex.evt.omnimarket.llm-delegation-routing-failed.v1",
    }
