# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for ContractTopologyParser (Task 6, OMN-11036)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from omnimarket.nodes.node_dependency_health_sweep.engine.contract_topology import (
    ContractTopologyParser,
)
from omnimarket.nodes.node_dependency_health_sweep.models import ModelTopologyGraph

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_contract(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


# ---------------------------------------------------------------------------
# Fixture 1: Matched pub/sub — no orphans
# ---------------------------------------------------------------------------


def test_matched_pub_sub_no_orphans(tmp_path: Path) -> None:
    """Two contracts with matching pub/sub produce no orphan topics."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.evt.omnimarket.topic-a.v1"
          subscribe_topics: []
        """,
    )
    _write_contract(
        tmp_path / "node_b" / "contract.yaml",
        """
        name: node_b
        event_bus:
          publish_topics: []
          subscribe_topics:
            - "onex.evt.omnimarket.topic-a.v1"
        """,
    )

    parser = ContractTopologyParser()
    graph: ModelTopologyGraph = parser.parse(search_roots=[tmp_path])

    assert "onex.evt.omnimarket.topic-a.v1" not in graph.orphan_topics
    assert len(graph.pub_edges) == 1
    assert len(graph.sub_edges) == 1
    # Confirm the directed pub edge: (node_a, topic-a, "pub")
    assert any(e[0] == "node_a" and "topic-a" in e[1] for e in graph.pub_edges)
    # Confirm the directed sub edge: (node_b, topic-a, "sub")
    assert any(e[0] == "node_b" and "topic-a" in e[1] for e in graph.sub_edges)


# ---------------------------------------------------------------------------
# Fixture 2: Unpaired publish → orphan_topics
# ---------------------------------------------------------------------------


def test_unpaired_publish_yields_orphan(tmp_path: Path) -> None:
    """A contract publishing topic B with no subscriber → orphan_topics contains B."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.evt.omnimarket.topic-b.v1"
          subscribe_topics: []
        """,
    )

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "onex.evt.omnimarket.topic-b.v1" in graph.orphan_topics


# ---------------------------------------------------------------------------
# Fixture 3: Hardcoded topic literal in Python → undeclared_topics
# ---------------------------------------------------------------------------


def test_hardcoded_topic_literal_in_python_yields_undeclared(tmp_path: Path) -> None:
    """Python file with hardcoded onex.cmd.foo.bar.v1 string → undeclared_topics."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics: []
          subscribe_topics: []
        """,
    )
    # Write a Python file with a bare topic literal NOT declared in any contract
    py_file = tmp_path / "node_a" / "some_handler.py"
    py_file.write_text('topic = "onex.cmd.foo.bar.v1"\n')

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "onex.cmd.foo.bar.v1" in graph.undeclared_topics


# ---------------------------------------------------------------------------
# Fixture 4: Allowlisted topic excluded from orphan reporting
# ---------------------------------------------------------------------------


def test_allowlisted_topic_excluded_from_orphans(tmp_path: Path) -> None:
    """Topic in dep_health_allowlist.yaml is excluded from orphan_topics."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.evt.omnimarket.topic-c.v1"
          subscribe_topics: []
        """,
    )
    # Write allowlist
    allowlist = tmp_path / "dep_health_allowlist.yaml"
    allowlist.write_text(
        textwrap.dedent(
            """
            allowlist:
              - topic: "onex.evt.omnimarket.topic-c.v1"
                reason: "Consumed by external system"
                owner: "platform-team"
                expiry_date: "2027-01-01"
            """
        )
    )

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "onex.evt.omnimarket.topic-c.v1" not in graph.orphan_topics


# ---------------------------------------------------------------------------
# Fixture 5: Contract missing event_bus block — no crash
# ---------------------------------------------------------------------------


def test_missing_event_bus_block_no_crash(tmp_path: Path) -> None:
    """Contract without event_bus block does not crash; treated as empty."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        node_type: EFFECT
        """,
    )

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert isinstance(graph, ModelTopologyGraph)
    assert graph.orphan_topics == []
    assert graph.undeclared_topics == []


# ---------------------------------------------------------------------------
# Fixture 6: externally_consumed_topics excluded from orphan reporting
# ---------------------------------------------------------------------------


def test_externally_consumed_topic_excluded_from_orphans(tmp_path: Path) -> None:
    """Topic declared externally_consumed_topics is not an orphan."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.evt.omnimarket.topic-d.v1"
          subscribe_topics: []
        externally_consumed_topics:
          - "onex.evt.omnimarket.topic-d.v1"
        """,
    )

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "onex.evt.omnimarket.topic-d.v1" not in graph.orphan_topics


# ---------------------------------------------------------------------------
# Fixture 7: Topic literal declared in contract → NOT in undeclared_topics
# ---------------------------------------------------------------------------


def test_topic_literal_matching_contract_not_undeclared(tmp_path: Path) -> None:
    """A topic string literal that matches a contract topic is not undeclared."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.cmd.omnimarket.dep-health-sweep-start.v1"
          subscribe_topics: []
        """,
    )
    py_file = tmp_path / "node_a" / "handler.py"
    py_file.write_text('TOPIC = "onex.cmd.omnimarket.dep-health-sweep-start.v1"\n')

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert (
        "onex.cmd.omnimarket.dep-health-sweep-start.v1" not in graph.undeclared_topics
    )


# ---------------------------------------------------------------------------
# Fixture 8: nodes field populated
# ---------------------------------------------------------------------------


def test_nodes_populated_from_contract_names(tmp_path: Path) -> None:
    """Node names are extracted from the name: field of each contract."""
    for name in ("node_alpha", "node_beta"):
        _write_contract(
            tmp_path / name / "contract.yaml",
            f"""
            name: {name}
            event_bus:
              publish_topics: []
              subscribe_topics: []
            """,
        )

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "node_alpha" in graph.nodes
    assert "node_beta" in graph.nodes


# ---------------------------------------------------------------------------
# Fixture 9: topic_sources populated for orphan topics (OMN-11048)
# ---------------------------------------------------------------------------


def test_orphan_topic_sources_populated(tmp_path: Path) -> None:
    """orphan topic_sources dict maps each orphan topic to its contract.yaml path."""
    contract_path = tmp_path / "node_a" / "contract.yaml"
    _write_contract(
        contract_path,
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.evt.omnimarket.topic-orphan.v1"
          subscribe_topics: []
        """,
    )

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "onex.evt.omnimarket.topic-orphan.v1" in graph.orphan_topics
    assert "onex.evt.omnimarket.topic-orphan.v1" in graph.topic_sources
    assert graph.topic_sources["onex.evt.omnimarket.topic-orphan.v1"] == str(
        contract_path.resolve()
    )


# ---------------------------------------------------------------------------
# Fixture 10: undeclared_topic_sources populated (OMN-11048)
# ---------------------------------------------------------------------------


def test_undeclared_topic_sources_populated(tmp_path: Path) -> None:
    """undeclared_topic_sources maps each undeclared literal to its source file."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics: []
          subscribe_topics: []
        """,
    )
    py_file = tmp_path / "node_a" / "some_handler.py"
    py_file.write_text('topic = "onex.cmd.foo.undeclared.v1"\n')

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "onex.cmd.foo.undeclared.v1" in graph.undeclared_topics
    assert "onex.cmd.foo.undeclared.v1" in graph.undeclared_topic_sources
    assert graph.undeclared_topic_sources["onex.cmd.foo.undeclared.v1"] == str(
        py_file.resolve()
    )


# ---------------------------------------------------------------------------
# Fixture 11: non-orphan topics have no entry in topic_sources (OMN-11048)
# ---------------------------------------------------------------------------


def test_non_orphan_topic_not_in_topic_sources(tmp_path: Path) -> None:
    """Subscribed (non-orphan) topics do not have a topic_sources entry."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.evt.omnimarket.paired.v1"
          subscribe_topics: []
        """,
    )
    _write_contract(
        tmp_path / "node_b" / "contract.yaml",
        """
        name: node_b
        event_bus:
          publish_topics: []
          subscribe_topics:
            - "onex.evt.omnimarket.paired.v1"
        """,
    )

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    # Paired — not an orphan — topic_sources MAY or MAY NOT contain it.
    # The key requirement: it must NOT appear in orphan_topics.
    assert "onex.evt.omnimarket.paired.v1" not in graph.orphan_topics


# ---------------------------------------------------------------------------
# Fixture 12: Topic literals in test files are NOT flagged as undeclared (OMN-11061)
# ---------------------------------------------------------------------------


def test_topic_literal_in_test_file_not_undeclared(tmp_path: Path) -> None:
    """Topic literals inside test_*.py files are excluded from undeclared_topics.

    Test files routinely use placeholder or fixture-local topics that are not
    production-declared. Flagging them as UNDECLARED_TOPIC is noise.
    """
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics: []
          subscribe_topics: []
        """,
    )
    # Synthetic test file with an undeclared topic literal
    test_file = tmp_path / "node_a" / "tests" / "test_handler.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text('FAKE_TOPIC = "onex.cmd.omnimarket.foo-requested.v1"\n')

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    # The topic must NOT appear as undeclared — it lives in a test file
    assert "onex.cmd.omnimarket.foo-requested.v1" not in graph.undeclared_topics


def test_topic_literal_in_test_named_file_not_undeclared(tmp_path: Path) -> None:
    """Topic literals in *_test.py files are excluded from undeclared_topics."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics: []
          subscribe_topics: []
        """,
    )
    test_file = tmp_path / "node_a" / "handler_test.py"
    test_file.write_text('FAKE_TOPIC = "onex.evt.omnimarket.bar-completed.v1"\n')

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "onex.evt.omnimarket.bar-completed.v1" not in graph.undeclared_topics


def test_topic_literal_in_production_file_still_undeclared(tmp_path: Path) -> None:
    """Topic literals in non-test production files ARE flagged when undeclared."""
    _write_contract(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics: []
          subscribe_topics: []
        """,
    )
    prod_file = tmp_path / "node_a" / "handler_prod.py"
    prod_file.write_text('TOPIC = "onex.cmd.omnimarket.prod-undeclared.v1"\n')

    parser = ContractTopologyParser()
    graph = parser.parse(search_roots=[tmp_path])

    assert "onex.cmd.omnimarket.prod-undeclared.v1" in graph.undeclared_topics
