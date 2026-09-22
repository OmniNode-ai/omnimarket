# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 for OMN-18999: the projection is SUBSCRIBED, not merely declared.

The ticket's acceptance criterion draws a distinction that matters here, and
the first version of its falsifier missed it. "A contract subscribes to the
event carrying the reason" was already TRUE before any of this work:
``node_redeploy_orchestrator`` has subscribed to
``prod-promotion-gate-evaluated`` since OMN-13211. A test asserting only that
would have been green on the exact tree the 2026-09-20 measurement called
defective.

The distinction is what the subscriber DOES with the decision. An
ORCHESTRATOR consumes it to ROUTE -- it re-wraps the reason onto
``redeploy-completed`` and moves on, leaving nothing behind. A PROJECTION
consumes it to RECORD. The corrected criterion asks for the second, so these
tests assert a subscriber that also declares a relation to write the decision
into, which is a property an orchestrator cannot accidentally satisfy.

The positive control is the one the original measurement used: the delegation
event family, whose failure event has three subscribing projection contracts
in this same repository. It is asserted here so a green result above cannot be
vacuous -- if the enumeration itself broke, the control goes red too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"

GATE_DECISION_TOPIC = "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1"
DELEGATION_FAILED_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"

#: The relation a promotion-gate decision must land in. Named here rather than
#: read from the node under test: a test that read the table name out of the
#: same contract it is checking would pass for any name at all, including a
#: typo, because both sides would move together.
GATE_DECISION_TABLE = "prod_promotion_gate_decisions"


def _contracts() -> dict[str, dict[str, Any]]:
    """Every node contract in the repository, keyed by its directory name.

    Enumerated from the tree rather than from a list, so a node added without
    being registered anywhere is still seen -- and so a broken enumeration
    shows up as an empty result the guard below refuses, rather than as a
    green pass over nothing.
    """
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(_NODES.glob("*/contract.yaml")):
        with open(path) as handle:
            loaded = yaml.safe_load(handle)
        if isinstance(loaded, dict):
            found[path.parent.name] = loaded
    return found


def _subscribers(topic: str) -> dict[str, dict[str, Any]]:
    """Contracts whose ``event_bus.subscribe_topics`` names this topic."""
    return {
        name: contract
        for name, contract in _contracts().items()
        if topic in (contract.get("event_bus") or {}).get("subscribe_topics", [])
    }


def _written_tables(contract: dict[str, Any]) -> set[str]:
    """Relations a contract declares it WRITES.

    ``db_io.db_tables`` with write access is the mechanical difference between
    a consumer that records and one that merely routes. A node without the
    block is not dispatched to the write path at all (OMN-13083 / OMN-16690),
    so this is the same fact the runtime reads, not a proxy for it.
    """
    db_io = contract.get("db_io") or {}
    return {
        str(table.get("name"))
        for table in (db_io.get("db_tables") or [])
        if str(table.get("access", "")) in {"write", "read_write"}
    }


def test_the_enumeration_itself_finds_contracts() -> None:
    """A broken enumeration must not read as a clean tree (rule 16).

    Every assertion below is of the form "this set contains X". An
    enumeration returning nothing would make all of them fail loudly, but an
    enumeration returning a handful of files would make the NEGATIVE
    assertions vacuous. This pins the floor.
    """
    contracts = _contracts()
    assert len(contracts) > 100, (
        f"only {len(contracts)} contracts enumerated under {_NODES}; the scan is "
        "broken, and a zero-row result from it is not evidence of anything"
    )


def test_ac4_a_projection_contract_subscribes_to_the_gate_decision_topic() -> None:
    """AC4: a subscriber that RECORDS the decision, not one that routes it."""
    subscribers = _subscribers(GATE_DECISION_TOPIC)
    assert subscribers, (
        f"nothing in the repository subscribes to {GATE_DECISION_TOPIC}; the "
        "gate's typed reason reaches no consumer at all"
    )

    recording = {
        name: tables
        for name, contract in subscribers.items()
        if (tables := _written_tables(contract))
    }
    assert recording, (
        f"{sorted(subscribers)} subscribe to {GATE_DECISION_TOPIC}, but none "
        "declares a relation to write the decision into. A subscriber that "
        "only routes leaves a blocked promotion indistinguishable from a "
        "promotion nobody requested, which is the defect OMN-18999 closes."
    )
    assert any(GATE_DECISION_TABLE in tables for tables in recording.values()), (
        f"a subscriber records, but none writes {GATE_DECISION_TABLE}: "
        f"{ {name: sorted(t) for name, t in recording.items()} }"
    )


def test_ac4_the_routing_subscriber_alone_would_not_satisfy_this() -> None:
    """The distinction the corrected falsifier turns on, asserted directly.

    ``node_redeploy_orchestrator`` has subscribed to this topic since
    OMN-13211 and declares no relation. If this test ever fails because the
    orchestrator gained one, the test above stops being a meaningful
    criterion and both need rewriting -- so the failure is the right outcome,
    not a nuisance.
    """
    subscribers = _subscribers(GATE_DECISION_TOPIC)
    orchestrator = subscribers.get("node_redeploy_orchestrator")
    assert orchestrator is not None, (
        "the redeploy orchestrator must still subscribe to the gate decision; "
        "it is how an allowed promotion reaches the deploy"
    )
    assert not _written_tables(orchestrator), (
        "the redeploy orchestrator now declares a written relation, so "
        "'a subscriber that records' no longer distinguishes the projection "
        "from the router; rewrite AC4's falsifier"
    )


def test_ac4_positive_control_the_delegation_family_has_three_subscribers() -> None:
    """The control the 2026-09-20 measurement used, re-run here.

    The measurement's whole force came from a comparison: the identical grep
    returned three subscribing contracts for the delegation failure event and
    zero for the gate decision. Re-asserting the positive half is what keeps
    a green result above from being a property of a broken scan.
    """
    subscribers = _subscribers(DELEGATION_FAILED_TOPIC)
    assert len(subscribers) >= 3, (
        f"the positive control returned {len(subscribers)} subscribers for "
        f"{DELEGATION_FAILED_TOPIC}, not the three the measurement recorded: "
        f"{sorted(subscribers)}. The enumeration, not the wiring, is suspect."
    )
    assert any(_written_tables(contract) for contract in subscribers.values()), (
        "the control's subscribers declare no relation, so the control does "
        "not demonstrate the mechanism this ticket is reproducing"
    )


def test_the_gate_compute_still_publishes_what_the_projection_subscribes_to() -> None:
    """Both halves of the edge, so a rename cannot silently unwire it.

    A subscription to a topic nobody publishes is as silent as no
    subscription at all, and it reads healthy on every lag and watermark.
    """
    contracts = _contracts()
    gate = contracts["node_prod_promotion_gate_compute"]
    published = (gate.get("event_bus") or {}).get("publish_topics", [])
    assert GATE_DECISION_TOPIC in published
    assert gate.get("terminal_event") == GATE_DECISION_TOPIC

    projection = contracts["node_projection_prod_promotion_gate"]
    subscribed = (projection.get("event_bus") or {}).get("subscribe_topics", [])
    assert subscribed == [GATE_DECISION_TOPIC], (
        "the projection must subscribe to exactly the gate's terminal event; "
        f"got {subscribed}"
    )


def test_the_projection_declares_its_dead_letter_queue() -> None:
    """A projection that makes refusals durable must not lose one itself."""
    projection = _contracts()["node_projection_prod_promotion_gate"]
    dlq = (projection.get("event_bus") or {}).get("dlq_topics", [])
    assert dlq, (
        "a malformed decision would be logged and dropped, making this node a "
        "new silent-loss site of exactly the kind it exists to remove"
    )


def test_the_projection_declares_the_relation_and_its_creating_migration() -> None:
    """The declared grant must have a delivering migration beside it."""
    projection = _contracts()["node_projection_prod_promotion_gate"]
    tables = (projection.get("db_io") or {}).get("db_tables") or []
    assert len(tables) == 1
    table = tables[0]
    assert table["name"] == GATE_DECISION_TABLE
    assert table["schema"] == "omninode_internal"
    assert table["access"] == "write"

    migration = (
        _NODES / "node_projection_prod_promotion_gate/migrations" / table["migration"]
    )
    assert migration.is_file(), (
        f"contract names {table['migration']} as the creating migration and the "
        "file is absent; a declared relation nothing creates is an outage "
        "waiting for the first write"
    )
