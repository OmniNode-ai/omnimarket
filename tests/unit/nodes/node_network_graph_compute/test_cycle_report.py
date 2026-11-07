# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-22903): the graph keeps every direction of a repository pair, so a pair that imports
itself both ways reads one cycle. The injected break (the old graph's one direction per pair)
must read none, and the counts of a relation must match what the fixture planted."""

from __future__ import annotations

import pytest

from omnimarket.models.import_relations import ModelLabelRule
from omnimarket.nodes.node_network_graph_compute.handlers.handler_import_relation_compute import (
    HandlerImportRelationCompute,
)

from .relation_support import bare_overlay, edge, example_overlay, request

pytestmark = pytest.mark.unit

_RUN = HandlerImportRelationCompute().handle


def _two_repos_importing_each_other() -> tuple:
    return (
        edge("repo_a:a/one.py", "repo_b:b/one.py"),
        edge("repo_a:a/two.py", "repo_b:b/one.py"),
        edge("repo_b:b/one.py", "repo_a:a/one.py"),
    )


def test_two_repositories_importing_each_other_read_one_cycle() -> None:
    report = _RUN(request(_two_repos_importing_each_other(), bare_overlay()))
    assert len(report.cycles) == 1
    cycle = report.cycles[0]
    assert cycle.labels == ("repo_a", "repo_b")
    assert [(r.source, r.target) for r in cycle.relations] == [
        ("repo_a", "repo_b"),
        ("repo_b", "repo_a"),
    ]
    assert cycle.occurrences == 3
    assert [(r.key.source, r.key.target) for r in report.relations] == [
        ("repo_a", "repo_b"),
        ("repo_b", "repo_a"),
    ]


def test_control_one_direction_per_pair_reads_no_cycle() -> None:
    """The injected break: the old graph's stronger-direction-only pair must read empty."""
    report = _RUN(
        request(
            _two_repos_importing_each_other(),
            bare_overlay(),
            keep_stronger_direction_only=True,
        )
    )
    assert report.cycles == ()
    assert [(r.key.source, r.key.target) for r in report.relations] == [
        ("repo_a", "repo_b")
    ]


def test_counts_are_distinct_pairs_files_and_entities() -> None:
    edges = (
        edge("repo_a:a/one.py", "repo_b:b/one.py"),
        edge("repo_a:a/one.py", "repo_b:b/one.py"),  # one pair, listed twice
        edge("repo_a:a/two.py", "repo_b:b/one.py"),
        edge("repo_a:a/two.py", "repo_b:b/two.py"),
    )
    (relation,) = _RUN(request(edges, bare_overlay())).relations
    assert relation.occurrences == 3
    assert relation.files == 2
    assert relation.entities == 4


def test_an_acyclic_chain_and_a_self_relation_read_no_cycle() -> None:
    edges = (
        edge("a:x.py", "b:y.py"),
        edge("b:y.py", "c:z.py"),
        edge("c:z.py", "c:w.py"),  # inside one label: no relation
    )
    report = _RUN(request(edges, bare_overlay()))
    assert report.cycles == ()
    assert len(report.relations) == 2


def test_three_labels_in_a_ring_read_one_cycle_and_two_rings_read_two() -> None:
    ring = (
        edge("a:x.py", "b:x.py"),
        edge("b:x.py", "c:x.py"),
        edge("c:x.py", "a:x.py"),
    )
    assert [c.labels for c in _RUN(request(ring, bare_overlay())).cycles] == [
        ("a", "b", "c")
    ]
    pairs = (
        edge("a:x.py", "b:x.py"),
        edge("b:x.py", "a:x.py"),
        edge("c:x.py", "d:x.py"),
        edge("d:x.py", "c:x.py"),
        edge("b:x.py", "c:x.py"),  # a one-way link between the rings joins no cycle
    )
    assert [c.labels for c in _RUN(request(pairs, bare_overlay())).cycles] == [
        ("a", "b"),
        ("c", "d"),
    ]


def test_label_rules_split_one_repository_into_labels() -> None:
    overlay = example_overlay().model_copy(
        update={
            "label_rules": (
                ModelLabelRule(label="dispatch", repo="m", path_prefix="d/"),
                ModelLabelRule(label="ledger-session", repo="m", path_prefix="l/"),
            )
        }
    )
    edges = (
        edge("m:d/run.py", "m:l/row.py"),
        edge("m:l/row.py", "m:d/run.py"),
        edge("m:other/x.py", "m:d/run.py"),  # no rule: the repository name is the label
    )
    report = _RUN(request(edges, overlay))
    assert [c.labels for c in report.cycles] == [("dispatch", "ledger-session")]
    assert ("m", "dispatch") in {(r.key.source, r.key.target) for r in report.relations}


def test_one_request_gives_one_set_of_bytes_whatever_the_edge_order() -> None:
    edges = _two_repos_importing_each_other()
    one = _RUN(request(edges, bare_overlay()))
    two = _RUN(request(tuple(reversed(edges)), bare_overlay()))
    assert one.model_dump_json() == two.model_dump_json()
    assert one.digest() == two.digest()
