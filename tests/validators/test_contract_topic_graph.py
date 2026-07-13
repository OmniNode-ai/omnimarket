# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the static contract topic graph gate (OMN-14527).

Two things are being proven here, and the second is the one that matters.

1. The gate is GREEN on the real corpus against its frozen baseline, so it does
   not cry wolf on day one and get disabled.

2. The gate can go RED -- and specifically, it goes red against EXISTS-but-WRONG,
   not merely against absence. A check that only fires when something is MISSING
   is vacuous against the defect class that actually ships: a producer that exists
   and is wired and is green, but publishes the wrong topic. Both are proven below.

The golden-chain tests at the bottom are GENERATED from the graph, one per edge.
Nothing is enumerated by hand, because coverage-by-enumeration means the edge you
forgot to enumerate is the one that breaks you.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
import yaml

from omnimarket.validators.contract_topic_graph import (
    ModelTopicGraph,
    build_graph,
    evaluate_ratchet,
    find_defects,
    load_baseline,
    main,
    merge_base_accepted_keys,
    parse_contract,
)

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, package: str, node: str, contract: dict) -> Path:
    """Write a contract.yaml at the <pkg>/nodes/<node>/ path the runtime loads from."""
    path = tmp_path / package / "nodes" / node / "contract.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract))
    return path


def _graph(tmp_path: Path, external: dict[str, str] | None = None) -> ModelTopicGraph:
    roots = {
        "omnibase_infra": tmp_path / "omnibase_infra",
        "omnimarket": tmp_path / "omnimarket",
    }
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    return build_graph(roots=roots, external_producers=external or {})


# ---------------------------------------------------------------------------
# GREEN CONTROL — a healthy edge must produce zero defects.
# ---------------------------------------------------------------------------


def test_healthy_producer_consumer_pair_is_clean(tmp_path: Path) -> None:
    """A producer and a consumer that agree on a topic is the whole happy path."""
    _write(
        tmp_path,
        "omnimarket",
        "node_emitter",
        {
            "name": "node_emitter",
            "runtime_dispatch": {"command_topic": "onex.cmd.omnimarket.emit.v1"},
            "event_bus": {"publish_topics": ["onex.evt.omnimarket.thing-happened.v1"]},
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "emit",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write(
        tmp_path,
        "omnimarket",
        "node_reader",
        {
            "name": "node_reader",
            "event_bus": {
                "subscribe_topics": ["onex.evt.omnimarket.thing-happened.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "read",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    graph = _graph(tmp_path)
    assert find_defects(graph) == []
    assert len(graph.edges()) == 1


# ---------------------------------------------------------------------------
# RED PROOF — the defect classes that actually shipped.
# ---------------------------------------------------------------------------


def test_orphaned_consumer_is_caught__the_ledger_defect(tmp_path: Path) -> None:
    """The real one: a perfectly-wired handler nothing can ever send a message to.

    This is node_ledger_write_effect. Its handler_routing is complete and correct
    (HandlerLedgerAppend, topic-routed). It subscribes. It is healthy. And no
    contract publishes onex.cmd.platform.ledger-append.v1, so it starved for its
    entire life while five tickets closed green on top of it.
    """
    _write(
        tmp_path,
        "omnibase_infra",
        "node_ledger_write_effect",
        {
            "name": "node_ledger_write_effect",
            "event_bus": {
                "subscribe_topics": ["onex.cmd.platform.ledger-append.v1"],
                "publish_topics": ["onex.evt.platform.ledger-appended.v1"],
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "ledger.append",
                        "topic": "onex.cmd.platform.ledger-append.v1",
                        "handler": {"name": "HandlerLedgerAppend"},
                    }
                ]
            },
        },
    )

    defects = find_defects(_graph(tmp_path))
    orphaned = [d for d in defects if d.defect == "ORPHANED_CONSUMER"]

    assert len(orphaned) == 1
    assert orphaned[0].node == "node_ledger_write_effect"
    assert orphaned[0].topic == "onex.cmd.platform.ledger-append.v1"

    # ...and its output goes nowhere either. Both halves of the dead ledger.
    assert any(
        d.defect == "ORPHANED_PRODUCER"
        and d.topic == "onex.evt.platform.ledger-appended.v1"
        for d in defects
    )


def test_red_against_exists_but_wrong__producer_publishes_the_wrong_topic(
    tmp_path: Path,
) -> None:
    """The non-vacuous RED proof.

    A gate that only fires on ABSENCE proves nothing about the defect that ships.
    Here the producer EXISTS, is runtime-loaded, has handler_routing, and publishes
    a real topic -- it just publishes the WRONG one (a v2 the consumer never asked
    for). Every per-node check is green. The graph still catches it, because the
    edge does not close.
    """
    _write(
        tmp_path,
        "omnimarket",
        "node_emitter",
        {
            "name": "node_emitter",
            "runtime_dispatch": {"command_topic": "onex.cmd.omnimarket.emit.v1"},
            # Consumer wants .v1. This publishes .v2. Nothing else is wrong.
            "event_bus": {"publish_topics": ["onex.evt.omnimarket.thing-happened.v2"]},
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "emit",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write(
        tmp_path,
        "omnimarket",
        "node_reader",
        {
            "name": "node_reader",
            "event_bus": {
                "subscribe_topics": ["onex.evt.omnimarket.thing-happened.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "read",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    defects = find_defects(_graph(tmp_path))

    # The consumer is starved even though a healthy, wired producer exists.
    assert any(
        d.defect == "ORPHANED_CONSUMER"
        and d.node == "node_reader"
        and d.topic == "onex.evt.omnimarket.thing-happened.v1"
        for d in defects
    ), "gate failed to catch EXISTS-but-WRONG — it is vacuous"
    # And the producer's output goes nowhere.
    assert any(
        d.defect == "ORPHANED_PRODUCER"
        and d.node == "node_emitter"
        and d.topic == "onex.evt.omnimarket.thing-happened.v2"
        for d in defects
    )


def test_declared_but_unwired_is_caught(tmp_path: Path) -> None:
    """Subscribes, receives, and drops: the runtime wires the subscription from
    subscribe_topics alone, so a node with no handler_routing consumes events and
    has nothing to dispatch them to."""
    _write(
        tmp_path,
        "omnibase_infra",
        "node_no_dispatch",
        {
            "name": "node_no_dispatch",
            "event_bus": {"subscribe_topics": ["onex.evt.platform.node-heartbeat.v1"]},
        },
    )
    _write(
        tmp_path,
        "omnibase_infra",
        "node_beat",
        {
            "name": "node_beat",
            "runtime_dispatch": {"command_topic": "onex.cmd.platform.beat.v1"},
            "event_bus": {"publish_topics": ["onex.evt.platform.node-heartbeat.v1"]},
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "beat",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    defects = find_defects(_graph(tmp_path))
    unwired = [d for d in defects if d.defect == "DECLARED_BUT_UNWIRED"]
    assert [d.node for d in unwired] == ["node_no_dispatch"]


def test_disconnected_subgraph_is_caught__two_rival_ledgers(tmp_path: Path) -> None:
    """A cluster that is internally wired perfectly and fed by nothing.

    This is how two complete rival ledger systems both sat idle: each was a
    self-consistent subgraph with no inbound edge from the platform.
    """
    _write(
        tmp_path,
        "omnimarket",
        "ledger_orchestrator",
        {
            "name": "ledger_orchestrator",
            "event_bus": {
                "subscribe_topics": ["onex.cmd.omnimarket.ledger-tick.v1"],
                "publish_topics": ["onex.evt.omnimarket.ledger-reduced.v1"],
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "tick",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write(
        tmp_path,
        "omnimarket",
        "ledger_state_reducer",
        {
            "name": "ledger_state_reducer",
            "event_bus": {
                "subscribe_topics": ["onex.evt.omnimarket.ledger-reduced.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "reduce",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    defects = find_defects(_graph(tmp_path))
    subgraphs = [d for d in defects if d.defect == "DISCONNECTED_SUBGRAPH"]
    assert len(subgraphs) == 1
    assert "ledger_orchestrator" in subgraphs[0].detail
    assert "ledger_state_reducer" in subgraphs[0].detail


def _write_catalog(tmp_path: Path, package: str, integrations: list[dict]) -> Path:
    """Write a contracts/integrations/catalog.yaml under a synthetic package root."""
    path = tmp_path / package / "contracts" / "integrations" / "catalog.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"integrations": integrations}))
    return path


def test_ondemand_ingress_root_is_not_flagged_disconnected(tmp_path: Path) -> None:
    """OMN-14591: a cron/on-demand root with zero subscriptions is NOT debt --
    but ONLY when a real, positive external-invocation signal backs it up.

    The motivating case: node_gmail_intent_poller_effect (subscribe_topics: [],
    "On-demand trigger" per its own contract comment, AND a real
    contracts/integrations/catalog.yaml poller entry) feeds
    node_gmail_intent_evaluator_effect over a real, correctly-matched topic.
    A node with no subscriptions has no possible way to ever run except by
    being invoked from outside the Kafka graph entirely -- that CAN be the
    entry point, even though neither original signal (command_topic,
    externally-fed subscribe_topics) can fire for a node with no
    subscriptions to examine. But verify round 1 proved the bare SHAPE alone
    is not sufficient (it also matched genuinely dead scaffolding with no
    invocation path at all -- see
    test_bare_ingress_shape_without_a_signal_is_still_flagged_disconnected
    below), so this fixture uses the explicit
    runtime_dispatch.external_trigger annotation as the positive signal (the
    catalog.yaml path is proven separately in
    test_catalog_poller_entry_is_a_valid_ingress_signal). Before either
    signal existed, this 2-node cluster read identically to a genuinely dead
    island (see test_disconnected_subgraph_is_caught__two_rival_ledgers just
    above, which must keep failing).
    """
    _write(
        tmp_path,
        "omnimarket",
        "ingress_poller",
        {
            "name": "ingress_poller",
            "event_bus": {
                "subscribe_topics": [],
                "publish_topics": ["onex.evt.omnimarket.signal-received.v1"],
            },
            "runtime_dispatch": {"external_trigger": True},
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "poll",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write(
        tmp_path,
        "omnimarket",
        "signal_evaluator",
        {
            "name": "signal_evaluator",
            "event_bus": {
                "subscribe_topics": ["onex.evt.omnimarket.signal-received.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "evaluate",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    defects = find_defects(_graph(tmp_path))
    assert [d for d in defects if d.defect == "DISCONNECTED_SUBGRAPH"] == []
    # Not orphaned either -- the topic IS produced and IS consumed.
    assert [
        d for d in defects if d.defect in ("ORPHANED_CONSUMER", "ORPHANED_PRODUCER")
    ] == []


def test_catalog_poller_entry_is_a_valid_ingress_signal(tmp_path: Path) -> None:
    """The OTHER positive signal: a real contracts/integrations/catalog.yaml
    poller entry, with NO explicit contract annotation -- exactly the shape
    of the real node_gmail_intent_poller_effect (its own catalog entry, no
    runtime_dispatch.external_trigger field at all).
    """
    _write(
        tmp_path,
        "omnimarket",
        "catalog_poller",
        {
            "name": "catalog_poller",
            "event_bus": {
                "subscribe_topics": [],
                "publish_topics": ["onex.evt.omnimarket.catalog-signal.v1"],
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "poll",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write(
        tmp_path,
        "omnimarket",
        "catalog_signal_evaluator",
        {
            "name": "catalog_signal_evaluator",
            "event_bus": {
                "subscribe_topics": ["onex.evt.omnimarket.catalog-signal.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "evaluate",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write_catalog(
        tmp_path,
        "omnimarket",
        [
            {
                "id": "catalog_poller_integration",
                "type": "poller",
                "nodes": ["catalog_poller"],
            }
        ],
    )

    defects = find_defects(_graph(tmp_path))
    assert [d for d in defects if d.defect == "DISCONNECTED_SUBGRAPH"] == []


def test_bare_ingress_shape_without_a_signal_is_still_flagged_disconnected(
    tmp_path: Path,
) -> None:
    """THE critical negative fixture from verify round 1.

    Same structural shape as the gmail poller (subscribe_topics == (), has
    publish_topics, feeds a real downstream consumer) but with NEITHER
    positive signal -- no catalog.yaml entry, no explicit annotation. This is
    exactly the shape of node_baseline_capture / node_pattern_lifecycle_effect:
    dead scaffolding with no invocation path, not a legitimate ingress root.
    Bare shape must NOT be treated as sufficient proof on its own.
    """
    _write(
        tmp_path,
        "omnimarket",
        "unsignaled_producer",
        {
            "name": "unsignaled_producer",
            "event_bus": {
                "subscribe_topics": [],
                "publish_topics": ["onex.evt.omnimarket.unclaimed-signal.v1"],
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "produce",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write(
        tmp_path,
        "omnimarket",
        "unsignaled_consumer",
        {
            "name": "unsignaled_consumer",
            "event_bus": {
                "subscribe_topics": ["onex.evt.omnimarket.unclaimed-signal.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "consume",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    defects = find_defects(_graph(tmp_path))
    subgraphs = [d for d in defects if d.defect == "DISCONNECTED_SUBGRAPH"]
    assert len(subgraphs) == 1
    assert "unsignaled_producer" in subgraphs[0].detail
    assert "unsignaled_consumer" in subgraphs[0].detail


def test_ingress_root_fix_does_not_mask_a_producer_nobody_consumes(
    tmp_path: Path,
) -> None:
    """The ingress-root exemption is narrow: it does not launder a producer
    whose output genuinely goes nowhere. That defect is ORPHANED_PRODUCER's
    job, evaluated independently of the subgraph check, and must still fire.
    """
    _write(
        tmp_path,
        "omnimarket",
        "ingress_poller_alone",
        {
            "name": "ingress_poller_alone",
            "event_bus": {
                "subscribe_topics": [],
                "publish_topics": ["onex.evt.omnimarket.nobody-listens.v1"],
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "poll",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    defects = find_defects(_graph(tmp_path))
    # Singleton component (no edges at all) -- covered by the orphan check,
    # never by the subgraph check (len(component) < 2 excludes it there).
    assert [d for d in defects if d.defect == "DISCONNECTED_SUBGRAPH"] == []
    orphaned = [d for d in defects if d.defect == "ORPHANED_PRODUCER"]
    assert len(orphaned) == 1
    assert orphaned[0].node == "ingress_poller_alone"


def test_ingress_root_rescues_only_its_own_cluster__a_separate_dead_island_still_fails(
    tmp_path: Path,
) -> None:
    """The discriminating negative fixture the fix must not blur.

    One graph, two unrelated weakly-connected components: the ingress-root
    pair (legitimately live, must NOT be flagged) sits ALONGSIDE a genuinely
    dead rival-ledgers-shaped island (two rival_* nodes, internally wired,
    fed by nothing -- must STILL be flagged). If the ingress-root signal ever
    leaked reachability across components, or if the fix accidentally
    softened the subgraph check in general rather than adding one narrow
    signal, the second island would go silent too. It must not.
    """
    _write(
        tmp_path,
        "omnimarket",
        "ingress_poller",
        {
            "name": "ingress_poller",
            "event_bus": {
                "subscribe_topics": [],
                "publish_topics": ["onex.evt.omnimarket.signal-received.v1"],
            },
            "runtime_dispatch": {"external_trigger": True},
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "poll",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write(
        tmp_path,
        "omnimarket",
        "signal_evaluator",
        {
            "name": "signal_evaluator",
            "event_bus": {
                "subscribe_topics": ["onex.evt.omnimarket.signal-received.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "evaluate",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    # A SEPARATE, unconnected component: internally wired, fed by nothing --
    # neither node has empty subscribe_topics, so the ingress-root signal
    # cannot fire for either one. Same shape as the rival-ledgers test above.
    _write(
        tmp_path,
        "omnimarket",
        "rival_orchestrator",
        {
            "name": "rival_orchestrator",
            "event_bus": {
                "subscribe_topics": ["onex.cmd.omnimarket.rival-tick.v1"],
                "publish_topics": ["onex.evt.omnimarket.rival-reduced.v1"],
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "tick",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    _write(
        tmp_path,
        "omnimarket",
        "rival_state_reducer",
        {
            "name": "rival_state_reducer",
            "event_bus": {"subscribe_topics": ["onex.evt.omnimarket.rival-reduced.v1"]},
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "reduce",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    defects = find_defects(_graph(tmp_path))
    subgraphs = {d.node for d in defects if d.defect == "DISCONNECTED_SUBGRAPH"}

    # The ingress-root pair is legitimately live -- rescued.
    assert "ingress_poller" not in subgraphs
    assert "signal_evaluator" not in subgraphs
    # The unrelated dead island is untouched by the fix -- still flagged.
    assert "rival_orchestrator" in subgraphs
    assert len([d for d in defects if d.defect == "DISCONNECTED_SUBGRAPH"]) == 1


def test_declared_external_producer_makes_a_consumer_reachable(tmp_path: Path) -> None:
    """Some topics really are published off-graph (the skill CLI, a GitHub webhook).

    That is legitimate, but it must be DECLARED, never assumed. An undeclared
    external producer is indistinguishable from a starved consumer -- which is
    exactly why the ledger sat dead for months.
    """
    _write(
        tmp_path,
        "omnimarket",
        "node_webhook_reader",
        {
            "name": "node_webhook_reader",
            "event_bus": {"subscribe_topics": ["onex.evt.github.pr-webhook.v1"]},
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "read",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )

    assert [d.defect for d in find_defects(_graph(tmp_path))] == ["ORPHANED_CONSUMER"]

    declared = _graph(
        tmp_path,
        external={"onex.evt.github.pr-webhook.v1": "GitHub webhook -> gateway ingress"},
    )
    assert find_defects(declared) == []


# ---------------------------------------------------------------------------
# THE PARSER — a shape it cannot read is a defect it cannot see.
# ---------------------------------------------------------------------------


def test_parser_reads_every_topic_declaration_shape(tmp_path: Path) -> None:
    """The corpus declares topics through many key shapes that accreted over time.

    A parser that knew only event_bus.publish_topics would miss ~40% of them and
    report the resulting phantom orphans as defects. Every shape below is real and
    in use today.
    """
    path = _write(
        tmp_path,
        "omnimarket",
        "node_many_shapes",
        {
            # The contract name is authoritative and is NOT the directory name.
            "name": "the_real_name",
            "event_bus": {
                "publish_topics": ["onex.evt.omnimarket.a.v1"],
                "publish": {
                    "success_topic": "onex.evt.omnimarket.b.v1",
                    "failure_topic": "onex.evt.omnimarket.c.v1",
                },
                "subscribe_topics": ["onex.cmd.omnimarket.d.v1"],
                "subscribe": {"topic": "onex.cmd.omnimarket.e.v1"},
            },
            "runtime_dispatch": {
                "command_topic": "onex.cmd.omnimarket.f.v1",
                "terminal_events": {
                    "success": "onex.evt.omnimarket.g.v1",
                    "failure": "onex.evt.omnimarket.h.v1",
                },
            },
            "published_events": [{"topic": "onex.evt.omnimarket.i.v1"}],
            "consumed_events": [{"topic": "onex.evt.omnimarket.j.v1"}],
        },
    )

    node = parse_contract(path, "omnimarket")
    assert node is not None
    assert node.name == "the_real_name"
    assert set(node.publish_topics) == {
        "onex.evt.omnimarket.a.v1",
        "onex.evt.omnimarket.b.v1",
        "onex.evt.omnimarket.c.v1",
        "onex.evt.omnimarket.g.v1",
        "onex.evt.omnimarket.h.v1",
        "onex.evt.omnimarket.i.v1",
    }
    assert set(node.subscribe_topics) == {
        "onex.cmd.omnimarket.d.v1",
        "onex.cmd.omnimarket.e.v1",
        "onex.cmd.omnimarket.f.v1",
        "onex.evt.omnimarket.j.v1",
    }
    assert node.command_topic == "onex.cmd.omnimarket.f.v1"


def test_build_graph_fails_closed_on_partial_package_surface(tmp_path: Path) -> None:
    """A partial graph is worse than no graph: every cross-package producer becomes
    a phantom orphan. Refuse to answer rather than answer wrongly."""
    (tmp_path / "omnimarket").mkdir()
    with pytest.raises(RuntimeError, match="UNSOUND"):
        build_graph(roots={"omnimarket": tmp_path / "omnimarket"})


# ---------------------------------------------------------------------------
# THE RATCHET — the baseline may only ever shrink.
# ---------------------------------------------------------------------------


def test_gate_is_green_on_the_real_corpus_against_its_baseline() -> None:
    """GREEN CONTROL. If this ever fails on an untouched tree the gate is crying
    wolf, and a gate that cries wolf on day one gets disabled by lunchtime."""
    assert main([]) == 0


def test_ratchet_hard_fails_on_a_new_defect(tmp_path: Path) -> None:
    """A NEW orphaned consumer must fail even though 744 identical ones are baselined."""
    baseline = tmp_path / "baseline.yaml"
    baseline.write_text(yaml.safe_dump({"external_producers": {}, "accepted": []}))

    _write(
        tmp_path,
        "omnibase_infra",
        "node_starved",
        {
            "name": "node_starved",
            "event_bus": {
                "subscribe_topics": ["onex.cmd.platform.nobody-sends-this.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "x",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    graph = _graph(tmp_path)
    defects = find_defects(graph)
    accepted = set(load_baseline(baseline).accepted)

    new = [d for d in defects if d.key() not in accepted]
    assert [d.node for d in new] == ["node_starved"]


def test_ratchet_hard_fails_when_a_baselined_defect_is_fixed(tmp_path: Path) -> None:
    """A baselined defect that gets FIXED must leave the baseline.

    Otherwise the stale entry silently re-authorizes the defect the day it
    regresses -- the baseline would quietly become a permanent exemption instead
    of a burn-down list.
    """
    baseline = tmp_path / "baseline.yaml"
    baseline.write_text(
        yaml.safe_dump(
            {
                "external_producers": {},
                "accepted": ["ORPHANED_CONSUMER::node_gone::onex.cmd.platform.gone.v1"],
            }
        )
    )
    graph = _graph(tmp_path)  # empty graph: the baselined defect no longer exists
    current = {d.key() for d in find_defects(graph)}
    fixed = set(load_baseline(baseline).accepted) - current
    assert fixed == {"ORPHANED_CONSUMER::node_gone::onex.cmd.platform.gone.v1"}


def test_ratchet_rejects_a_defect_smuggled_in_via_its_own_baseline_entry(
    tmp_path: Path,
) -> None:
    """A PR cannot launder a new defect by baselining it in the same commit.

    ``evaluate_ratchet`` must key off ``trusted_accepted`` (the merge-base
    baseline), never the PR-local one -- otherwise a PR could add a real
    defect and its exact baseline key together and ``new_defects`` would
    come back empty, defeating the entire shrink-only ratchet.
    """
    _write(
        tmp_path,
        "omnibase_infra",
        "node_starved",
        {
            "name": "node_starved",
            "event_bus": {
                "subscribe_topics": ["onex.cmd.platform.nobody-sends-this.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "x",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    findings = find_defects(_graph(tmp_path))
    key = next(f.key() for f in findings if f.node == "node_starved")

    # The PR's own baseline already accepts the key -- as if the implementer
    # ran --write-baseline in the same commit that introduced the defect.
    local_accepted = {key}
    # The merge-base baseline (what actually predates this PR) does not.
    trusted_accepted: set[str] = set()

    new_defects, fixed = evaluate_ratchet(findings, local_accepted, trusted_accepted)

    assert [f.key() for f in new_defects] == [key]
    assert fixed == []


def test_ratchet_trusts_a_defect_that_genuinely_predates_the_pr(
    tmp_path: Path,
) -> None:
    """The same key is silent when the MERGE-BASE baseline already has it."""
    _write(
        tmp_path,
        "omnibase_infra",
        "node_starved",
        {
            "name": "node_starved",
            "event_bus": {
                "subscribe_topics": ["onex.cmd.platform.nobody-sends-this.v1"]
            },
            "handler_routing": {
                "handlers": [
                    {
                        "operation": "x",
                        "handler": {"module": "test_module", "name": "TestHandler"},
                    }
                ]
            },
        },
    )
    findings = find_defects(_graph(tmp_path))
    key = next(f.key() for f in findings if f.node == "node_starved")

    local_accepted = {key}
    trusted_accepted = {key}  # genuinely pre-existing, per the merge-base baseline

    new_defects, fixed = evaluate_ratchet(findings, local_accepted, trusted_accepted)

    assert new_defects == []
    assert fixed == []


def test_merge_base_accepted_keys_returns_none_outside_a_git_repo(
    tmp_path: Path,
) -> None:
    """No ``.git`` reachable from the baseline path -- resolve to unknown, not []."""
    baseline_path = tmp_path / "not_a_repo" / "baseline.yaml"
    baseline_path.parent.mkdir(parents=True)
    baseline_path.write_text(yaml.safe_dump({"external_producers": {}, "accepted": []}))

    assert merge_base_accepted_keys(baseline_path) is None


def test_baseline_is_a_burn_down_list_not_a_growing_allowlist() -> None:
    """The shipped baseline is frozen pre-existing debt. It exists to be deleted."""
    baseline = load_baseline(
        Path(__file__).parents[2]
        / "src/omnimarket/validators/data/contract_topic_graph_baseline.yaml"
    )
    assert baseline.accepted, (
        "baseline must record the pre-existing debt it is suppressing"
    )
    # Every entry is a real, resolvable defect key -- not a wildcard or a blanket.
    for key in baseline.accepted:
        assert key.count("::") == 2, (
            f"baseline entries are exact defect keys, got {key!r}"
        )


# ---------------------------------------------------------------------------
# GOLDEN-CHAIN TESTS — GENERATED, one per edge. Never hand-enumerated.
#
# Coverage-by-enumeration means the edge you forgot to enumerate is the one that
# breaks you. These are derived from the graph itself, so a new contract gets its
# chain test the moment it lands and coverage cannot be forgotten.
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _real_graph() -> ModelTopicGraph:
    """The real corpus graph. Built once — an rglob over every installed package is
    far too expensive to repeat per parametrized edge."""
    baseline = load_baseline(
        Path(__file__).parents[2]
        / "src/omnimarket/validators/data/contract_topic_graph_baseline.yaml"
    )
    return build_graph(external_producers=baseline.external_producers)


def _live_edges() -> list[object]:
    """One test case per declared edge, ratcheted against the baseline.

    An edge whose consumer is a KNOWN unwired node is marked xfail(strict=True):
    it stays visible as debt, and the moment someone fixes it the strict xfail
    turns red and forces the baseline entry to be removed. A NEW broken edge has
    no such mark and hard-fails immediately.

    These five are the sharpest defects the graph found, because unlike a starved
    consumer they have a LIVE producer: node_contract_registry_reducer subscribes
    to onex.evt.platform.node-heartbeat.v1 -- a topic carrying over a million
    messages -- and declares no handler_routing, so it has been consuming and
    silently dropping every single one.
    """
    graph = _real_graph()
    unwired = {
        n.name
        for n in graph.nodes
        if n.runtime_loaded and n.subscribe_topics and not n.has_dispatch_wiring
    }
    baselined = {
        key.split("::")[1]
        for key in load_baseline(
            Path(__file__).parents[2]
            / "src/omnimarket/validators/data/contract_topic_graph_baseline.yaml"
        ).accepted
        if key.startswith("DECLARED_BUT_UNWIRED::")
    }

    cases: list[object] = []
    for producer, topic, consumer in sorted(set(graph.edges())):
        if consumer in unwired and consumer in baselined:
            cases.append(
                pytest.param(
                    producer,
                    topic,
                    consumer,
                    marks=pytest.mark.xfail(
                        strict=True,
                        reason=(
                            f"{consumer} has a live producer but NO dispatcher — every "
                            "event on this chain is consumed and dropped (baselined debt)"
                        ),
                    ),
                )
            )
        else:
            cases.append(pytest.param(producer, topic, consumer))
    return cases


@pytest.mark.parametrize(("producer", "topic", "consumer"), _live_edges())
def test_golden_chain_edge_closes(producer: str, topic: str, consumer: str) -> None:
    """For every declared edge: the producer really publishes it, the consumer
    really subscribes to it, and the consumer can actually dispatch what arrives.

    An edge whose consumer has no dispatcher is a chain that terminates in a
    silent drop -- the event is consumed, then discarded, and nothing surfaces.
    """
    by_name = {n.name: n for n in _real_graph().nodes}

    assert topic in by_name[producer].publish_topics
    assert topic in by_name[consumer].subscribe_topics
    assert by_name[consumer].has_dispatch_wiring, (
        f"{consumer} consumes {topic} but declares no handler_routing/handler — "
        "the event arrives and is dropped"
    )
