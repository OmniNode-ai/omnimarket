# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 through the real graph: two repository snapshots that import each other are built by the code
graph, their edges go to the relation report, and the report reads one cycle."""

from __future__ import annotations

import pytest

from omnimarket.models.model_code_graph import ModelRepoSnapshot
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    Graph,
    GraphRules,
    build_graph,
    import_edges,
)
from omnimarket.nodes.node_network_graph_compute.handlers.handler_import_relation_compute import (
    HandlerImportRelationCompute,
)
from tests.nodes.node_code_graph_effect.code_graph_support import overlay

from .relation_support import bare_overlay, request

pytestmark = pytest.mark.unit

_SOURCE = "src/"


def _snapshot(repo: str, files: dict[str, str]) -> ModelRepoSnapshot:
    return ModelRepoSnapshot(repo=repo, ref="main", head=repo[0] * 40, files=files)


def _graph(*, back_import: bool) -> Graph:
    rules = GraphRules(roots=overlay().graph_roots)
    one = _snapshot(
        "repo_one",
        {
            "src/pkg_one/__init__.py": "",
            "src/pkg_one/alpha.py": "import pkg_two.beta\n",
            "src/pkg_one/gamma.py": "from pkg_two import beta\n",
        },
    )
    two = _snapshot(
        "repo_two",
        {
            "src/pkg_two/__init__.py": "",
            "src/pkg_two/beta.py": "import pkg_one.alpha\n" if back_import else "",
            "tests/test_beta.py": "import pkg_one.alpha\n",
        },
    )
    return build_graph([one, two], rules)


def test_the_graph_hands_both_directions_of_a_pair_to_the_relation_report() -> None:
    edges = import_edges(_graph(back_import=True))
    pairs = {(e.importer_repo, e.imported_repo) for e in edges}
    assert pairs == {("repo_one", "repo_two"), ("repo_two", "repo_one")}
    # an edge out of a test file is not an import of the code
    assert all(e.importer_path != "tests/test_beta.py" for e in edges)
    report = HandlerImportRelationCompute().handle(request(edges, bare_overlay()))
    assert [c.labels for c in report.cycles] == [("repo_one", "repo_two")]
    one_to_two = next(r for r in report.relations if r.key.source == "repo_one")
    assert one_to_two.files == 2


def test_control_without_the_back_import_reads_no_cycle() -> None:
    edges = import_edges(_graph(back_import=False))
    report = HandlerImportRelationCompute().handle(request(edges, bare_overlay()))
    assert report.cycles == ()
    assert [(r.key.source, r.key.target) for r in report.relations] == [
        ("repo_one", "repo_two")
    ]
