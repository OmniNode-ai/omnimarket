# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two backends of the graph verdict source: a fixed answer in memory, and a graph built from the repositories.

The real backend is a graph run of its own: it reads each declared repository at its ref through the code graph's
repository source, builds the graph with the code graph's rules and takes the verdict for each node the join names. It
writes nothing; the nightly graph's rows are the graph node's.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from omnimarket.models.model_code_graph import ModelRepoSnapshot
from omnimarket.models.model_join_j1 import (
    EnumGraphNodeVerdict,
    ModelGraphNodeVerdict,
    ModelGraphRunVerdicts,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    Graph,
    GraphRules,
    build_graph,
)
from omnimarket.nodes.node_code_graph_effect.protocols import (
    ProtocolRepoSource,
    RepoSourceError,
)
from omnimarket.nodes.node_host_reading_effect.overlay import ModelJoinJ1Node


class InMemoryGraphVerdictSource:
    """A graph run held in memory. ``run`` is answered under the correlation id the caller passes, so a test names it."""

    backend: Literal["in_memory", "real"] = "in_memory"

    def __init__(
        self,
        verdicts: Sequence[ModelGraphNodeVerdict],
        *,
        failure: str = "",
    ) -> None:
        self._verdicts = tuple(verdicts)
        self._failure = failure

    def read(self, correlation_id: str) -> ModelGraphRunVerdicts:
        if self._failure:
            return ModelGraphRunVerdicts(
                correlation_id=correlation_id,
                status="failed",
                failure=self._failure,
            )
        return ModelGraphRunVerdicts(
            correlation_id=correlation_id,
            status="completed",
            verdicts=self._verdicts,
        )


def node_verdict(graph: Graph, node: ModelJoinJ1Node) -> ModelGraphNodeVerdict:
    """Used when a root reaches any of the node's non-test modules, unused when none does, missing when it has none."""
    prefix = node.package_dir.strip("/") + "/"
    mine = {
        entity
        for entity in graph.modules - graph.tests
        if entity[0] == node.repo and entity[1].startswith(prefix)
    }
    reached = len(mine & graph.reached)
    if not mine:
        verdict = EnumGraphNodeVerdict.MISSING
    elif reached:
        verdict = EnumGraphNodeVerdict.USED
    else:
        verdict = EnumGraphNodeVerdict.UNUSED
    return ModelGraphNodeVerdict(
        node=node.name, verdict=verdict, modules=len(mine), reached=reached
    )


class GraphBuildingVerdictSource:
    """Build the graph of the declared repositories at their refs and answer the verdict of each node."""

    backend: Literal["in_memory", "real"] = "real"

    def __init__(
        self,
        *,
        source: ProtocolRepoSource,
        rules: GraphRules,
        repos: Sequence[tuple[str, str]],
        nodes: Sequence[ModelJoinJ1Node],
    ) -> None:
        self._source = source
        self._rules = rules
        self._repos = tuple(repos)
        self._nodes = tuple(nodes)

    def read(self, correlation_id: str) -> ModelGraphRunVerdicts:
        snapshots: list[ModelRepoSnapshot] = []
        for name, ref in self._repos:
            try:
                snapshots.append(self._source.snapshot(name, ref, self._rules.wanted))
            except RepoSourceError as exc:
                return ModelGraphRunVerdicts(
                    correlation_id=correlation_id,
                    status="failed",
                    failure=f"repo-unreadable:{name}:{exc}"[:200],
                )
        graph = build_graph(snapshots, self._rules)
        return ModelGraphRunVerdicts(
            correlation_id=correlation_id,
            status="completed",
            verdicts=tuple(node_verdict(graph, node) for node in self._nodes),
        )
