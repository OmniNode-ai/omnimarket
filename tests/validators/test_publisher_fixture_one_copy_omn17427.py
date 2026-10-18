# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fence: omnimarket resolves a publisher through the ONE fixture in omnibase_infra.

omnimarket once carried its own copy of the publisher contract fixture next to
omnibase_infra's, and the two read different sets of publisher declaration shapes (the
infra copy missed 96 topics omnimarket's parser reads as published). The index, the
parser and the alias derivation now live in
``omnibase_infra.testing.publisher_contract_fixture``; the omnimarket module only picks
the contracts. This file fails if a second parser or index comes back, or if the shared
fixture stops agreeing with the contract topic graph over omnimarket's own contracts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from omnibase_infra.testing import publisher_contract_fixture as infra_fixture

from omnimarket.testing import publisher_contract_fixture as market_fixture
from omnimarket.validators.contract_topic_graph import parse_contract

pytestmark = pytest.mark.unit

MARKET_SRC = Path(market_fixture.__file__).resolve().parents[1]


def _market_graph_publishers() -> dict[str, str]:
    """Every topic omnimarket's own parser reads as published, with its contract."""
    topics: dict[str, str] = {}
    for path in sorted(MARKET_SRC.rglob("contract.yaml")):
        node = parse_contract(path, "omnimarket")
        if node is None:
            continue
        for topic in node.publish_topics:
            topics.setdefault(topic, node.name)
        if node.command_topic is not None:
            topics.setdefault(node.command_topic, node.name)
    return topics


def test_the_graph_has_publishers_to_compare() -> None:
    # Positive control: a comparison over an empty set would pass for any fixture.
    assert len(_market_graph_publishers()) > 100


def test_every_topic_the_graph_reads_as_published_is_resolved_by_the_fixture() -> None:
    graph = _market_graph_publishers()
    corpus, scanned = market_fixture._corpus()
    assert scanned > 0
    missing = sorted(t for t in graph if not corpus.publishers_of(t))
    assert missing == [], (
        f"{len(missing)} of {len(graph)} topics omnimarket's parser reads as "
        f"published are not indexed by the shared fixture: {missing[:5]}"
    )


def test_the_module_uses_the_infra_corpus_class() -> None:
    corpus, _ = market_fixture._corpus()
    assert type(corpus) is infra_fixture.PublisherContractCorpus


def test_a_topic_nothing_publishes_is_still_refused() -> None:
    # Negative control for the fence above: the fixture is not accepting everything.
    with pytest.raises(market_fixture.NoDeclaredPublisherError):
        market_fixture.publisher_event_type("onex.evt.nothing.never-published.v1")
    assert (
        market_fixture.declared_publishers("onex.evt.nothing.never-published.v1") == ()
    )


def test_the_omnimarket_module_holds_no_parser_of_its_own() -> None:
    tree = ast.parse(Path(market_fixture.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("contract_topic_graph" in name for name in imported)
    assert "yaml" not in imported
    assert "omnibase_infra.testing.publisher_contract_fixture" in imported
