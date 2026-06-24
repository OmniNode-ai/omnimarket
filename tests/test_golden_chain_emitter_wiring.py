# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13545: regression guard for the pattern_learning / routing / evaluation
golden-chain head-topic emit paths.

Context
-------
The golden-chain sweep on the dev lane reported three chains producing zero
messages on their head topics:

    pattern_learning   head: onex.evt.omniintelligence.pattern-stored.v1
    routing            head: onex.evt.omniclaude.llm-routing-decision.v1
    evaluation         head: onex.evt.omniclaude.session-outcome.v1

Root cause (investigation, not a single omnimarket publish bug):

  * routing + evaluation -- the head topics ARE declared as emit-daemon
    fan-out targets in ``node_emit_daemon/registries/topics.yaml`` (the
    OMN-13146 canonical event registry). The producers are the omniclaude
    hooks (``llm.routing.decision`` / ``session.outcome`` emitted via the
    emit daemon). Dev-lane silence is a runtime/IPC condition (daemon not
    connected to the dev broker), not a missing omnimarket publish path.

  * pattern_learning -- the head topic is published *directly to the bus* by
    the omniintelligence ``node_pattern_storage_effect`` EFFECT node, so it is
    intentionally NOT an emit-daemon fan-out target. The omnimarket-side
    guarantee is that the projection subscribes to exactly that topic.

The silent failure mode this test locks out is *drift*: if the registry
fan-out for routing/session ever stops targeting the head topic the
projection consumes, the chain goes silent with no other signal. This test
pins each golden-chain head topic to its declared omnimarket-visible
emit/consume path so that drift fails CI instead of the dev lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry

_REPO_ROOT = Path(__file__).parent.parent
_EMIT_REGISTRY = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_emit_daemon"
    / "registries"
    / "topics.yaml"
)
_GOLDEN_CHAINS = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_golden_chain_sweep"
    / "golden_chains.yaml"
)

# Each golden-chain head topic and the emit-daemon logical event that fans out
# to it. ``None`` => the producer is a cross-repo EFFECT node publishing direct
# to the bus (omnimarket guarantee is the projection subscription, asserted
# separately), so it is intentionally absent from the emit-daemon registry.
_HEAD_TOPIC_EMIT_EVENT: dict[str, str | None] = {
    "onex.evt.omniclaude.llm-routing-decision.v1": "llm.routing.decision",
    "onex.evt.omniclaude.session-outcome.v1": "session.outcome",
    "onex.evt.omniintelligence.pattern-stored.v1": None,
}

# Golden-chain name -> projection node whose contract must subscribe to the
# chain head topic (the consume leg).
#
# NB: the ``routing`` golden chain (head llm-routing-decision.v1, tail
# llm_routing_decisions) is served by ``node_projection_llm_routing`` -- NOT by
# ``node_projection_routing_decision``, which is a *different* chain
# (head routing-decision.v1, tail agent_routing_decisions, the OMN-13122
# registration chain). Wiring the wrong projection node was the original
# confusion; this map pins the correct consumer for each chain.
_CHAIN_PROJECTION_NODE: dict[str, str] = {
    "routing": "node_projection_llm_routing",
    "evaluation": "node_projection_session_outcome",
    "pattern_learning": "node_projection_pattern_learning",
}


def _load_golden_chains() -> dict[str, str]:
    """Return {chain_name: head_topic} for the three chains under test."""
    raw = yaml.safe_load(_GOLDEN_CHAINS.read_text(encoding="utf-8"))
    chains = raw.get("chains") or raw.get("golden_chains") or raw
    if isinstance(chains, dict):
        chains = chains.get("chains", chains)
    out: dict[str, str] = {}
    for entry in chains:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        head = entry.get("head_topic")
        if name in _CHAIN_PROJECTION_NODE and head:
            out[name] = head
    return out


def _projection_subscribe_topics(node: str) -> list[str]:
    contract = _REPO_ROOT / "src" / "omnimarket" / "nodes" / node / "contract.yaml"
    raw = yaml.safe_load(contract.read_text(encoding="utf-8"))
    return list(raw.get("event_bus", {}).get("subscribe_topics", []))


@pytest.mark.unit
class TestGoldenChainEmitterWiring:
    """Lock the three OMN-13545 chains to declared emit/consume paths."""

    def test_golden_chain_head_topics_match_expected(self) -> None:
        """Head topics in golden_chains.yaml are the ones we guard here.

        Guards against the chain definitions silently renaming a head topic
        out from under the projection that consumes it.
        """
        chains = _load_golden_chains()
        assert chains["routing"] == "onex.evt.omniclaude.llm-routing-decision.v1"
        assert chains["evaluation"] == "onex.evt.omniclaude.session-outcome.v1"
        assert (
            chains["pattern_learning"] == "onex.evt.omniintelligence.pattern-stored.v1"
        )

    def test_emit_daemon_fans_out_to_routing_and_session_head_topics(self) -> None:
        """routing + evaluation head topics have a declared emit-daemon path.

        This is the omnimarket producer-side wiring guarantee: the canonical
        event registry must declare a fan-out rule whose target is exactly the
        golden-chain head topic. If this drifts, the chain goes silent.
        """
        registry = EventRegistry.from_yaml(_EMIT_REGISTRY)

        for head_topic, event_type in _HEAD_TOPIC_EMIT_EVENT.items():
            if event_type is None:
                # Cross-repo direct-to-bus producer; asserted by the
                # projection-subscription test instead.
                continue
            registration = registry.get_registration(event_type)
            assert registration is not None, (
                f"emit-daemon registry has no event {event_type!r} for head "
                f"topic {head_topic!r}"
            )
            fan_out_topics = {rule.topic for rule in registration.fan_out}
            assert head_topic in fan_out_topics, (
                f"event {event_type!r} does not fan out to head topic "
                f"{head_topic!r}; fan-out targets are {sorted(fan_out_topics)}"
            )

    def test_pattern_stored_is_cross_repo_direct_to_bus(self) -> None:
        """pattern-stored is intentionally NOT an emit-daemon fan-out target.

        Its producer is the omniintelligence node_pattern_storage_effect EFFECT
        node, which publishes direct to the bus. Locking this prevents a future
        change from wrongly adding an omnimarket emit-daemon producer for a
        topic that omnimarket does not own.
        """
        registry = EventRegistry.from_yaml(_EMIT_REGISTRY)
        all_fan_out_topics: set[str] = set()
        for event_type in registry.list_event_types():
            registration = registry.get_registration(event_type)
            if registration is None:
                continue
            all_fan_out_topics.update(rule.topic for rule in registration.fan_out)
        assert "onex.evt.omniintelligence.pattern-stored.v1" not in all_fan_out_topics

    def test_each_chain_projection_subscribes_to_head_topic(self) -> None:
        """Consume leg: every chain's projection subscribes to the head topic.

        This is the omnimarket-side guarantee for all three chains, including
        the cross-repo pattern-stored producer.
        """
        chains = _load_golden_chains()
        for chain_name, node in _CHAIN_PROJECTION_NODE.items():
            head_topic = chains[chain_name]
            subscribed = _projection_subscribe_topics(node)
            assert head_topic in subscribed, (
                f"projection {node!r} does not subscribe to chain {chain_name!r} "
                f"head topic {head_topic!r}; subscribes to {subscribed}"
            )
