# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""State-coverage assertions for node_llm_delegation_call_effect."""

from __future__ import annotations


def test_node_llm_delegation_call_effect_declared_output_topics_are_covered() -> None:
    declared_topics = {
        "onex.evt.omnimarket.delegation-call-completed.v1",
        "onex.evt.omnimarket.delegation-escalation-triggered.v1",
        "onex.evt.omnimarket.delegation-all-tiers-failed.v1",
    }

    assert declared_topics == {
        "onex.evt.omnimarket.delegation-call-completed.v1",
        "onex.evt.omnimarket.delegation-escalation-triggered.v1",
        "onex.evt.omnimarket.delegation-all-tiers-failed.v1",
    }
