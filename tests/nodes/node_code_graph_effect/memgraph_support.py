# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fake Bolt driver that holds the version store's graph in memory (OMN-26104).

It understands the statements of ``MemgraphVersionStore`` by their first words and keeps nodes by
label and key, so the store's write, read, idempotence and prune are exercised with no network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FakeBoltSession:
    def __init__(self, graph: FakeBoltDriver) -> None:
        self._graph = graph

    def close(self) -> None:
        return None

    def run(self, query: str, parameters: Mapping[str, Any] | None = None) -> Any:
        graph = self._graph
        graph.statements.append(query)
        if graph.fail is not None:
            raise graph.fail
        params = dict(parameters or {})
        if query.startswith("CREATE INDEX"):
            return []
        if "RETURN r.key AS key ORDER BY r.built_seq DESC LIMIT 1" in query:
            rows = graph.revisions(params["repo"])
            return [{"key": rows[-1]["key"]}] if rows else []
        if "RETURN r.key AS key, r.commit AS commit ORDER BY r.built_seq" in query:
            return [
                {"key": r["key"], "commit": r["commit"]}
                for r in graph.revisions(params["repo"])
            ]
        if "RETURN r.commit AS commit, r.committed_at AS committed_at" in query:
            return [
                {"commit": r["commit"], "committed_at": r.get("committed_at")}
                for r in graph.revisions(params["repo"])
            ]
        if "RETURN r.committed_at AS committed_at" in query:
            row = graph.nodes["CgRevision"].get(params["key"])
            return (
                [{"committed_at": row.get("committed_at")}]
                if row and row.get("complete")
                else []
            )
        if "RETURN r.repo AS repo, r.commit AS commit" in query:
            row = graph.nodes["CgRevision"].get(params["key"])
            return (
                [
                    {
                        "repo": row["repo"],
                        "commit": row["commit"],
                        "committed_at": row.get("committed_at"),
                    }
                ]
                if row and row.get("complete")
                else []
            )
        if query.startswith(
            "MATCH (r:CgRevision {key: $key, complete: true}) RETURN r.key"
        ):
            row = graph.nodes["CgRevision"].get(params["key"])
            return [{"key": row["key"]}] if row and row.get("complete") else []
        if query.startswith("MERGE (r:CgRevision"):
            row = graph.nodes["CgRevision"].setdefault(
                params["key"], {"key": params["key"], "built_seq": params["seq"]}
            )
            row.update(
                repo=params["repo"],
                commit=params["commit"],
                rev=params["key"],
                committed_at=params["committed_at"],
            )
            return []
        if "SET r.complete = true" in query:
            graph.nodes["CgRevision"][params["key"]].update(complete=True)
            return []
        if "DETACH DELETE" in query:
            label = query.split("(n:")[1].split(" ")[0]
            graph.nodes[label] = {
                k: v for k, v in graph.nodes[label].items() if v["rev"] != params["rev"]
            }
            graph.edges = [e for e in graph.edges if e["rev"] != params["rev"]]
            return []
        if "MERGE (m:CgModule" in query:
            for row in params["rows"]:
                graph.nodes["CgModule"][row["key"]] = {
                    "key": row["key"],
                    "path": row["path"],
                    "rev": params["rev"],
                }
            return []
        if "MERGE (s:CgSymbol" in query:
            for row in params["rows"]:
                graph.nodes["CgSymbol"][row["key"]] = {**row, "rev": params["rev"]}
            return []
        if "MERGE (a)-[i:CG_IMPORTS]" in query:
            for row in params["rows"]:
                edge = {**row, "type": "CG_IMPORTS", "rev": params["rev"]}
                if edge not in graph.edges:
                    graph.edges.append(edge)
            return []
        if "MERGE (h:CgShape" in query:
            for row in params["rows"]:
                graph.nodes["CgShape"][row["key"]] = {**row, "rev": params["rev"]}
            return []
        if "MERGE (e:CgExemplar" in query:
            for row in params["rows"]:
                graph.nodes["CgExemplar"][row["key"]] = {**row, "rev": params["rev"]}
            return []
        if "MATCH (s:CgSymbol {rev: $key})" in query:
            return [
                {k: s[k] for k in ("name", "kind", "path", "line")}
                for s in graph.nodes["CgSymbol"].values()
                if s["rev"] == params["key"]
            ]
        if "-[:CG_IMPORTS]->" in query:
            paths = {
                m["key"]: m["path"]
                for m in graph.nodes["CgModule"].values()
                if m["rev"] == params["key"]
            }
            return [
                {"importer": paths[e["source"]], "imported": paths[e["target"]]}
                for e in graph.edges
                if e["type"] == "CG_IMPORTS" and e["rev"] == params["key"]
            ]
        if "MATCH (h:CgShape {rev: $key})" in query:
            return [
                {k: h[k] for k in ("name", "path", "line", "signature", "fields")}
                for h in graph.nodes["CgShape"].values()
                if h["rev"] == params["key"]
            ]
        if "MATCH (e:CgExemplar {rev: $key})" in query:
            return [
                {k: e[k] for k in ("path", "name", "archetype")}
                for e in graph.nodes["CgExemplar"].values()
                if e["rev"] == params["key"]
            ]
        raise AssertionError(f"the fake Bolt session does not know: {query}")


class FakeBoltDriver:
    """Nodes by label and key, edges as dicts, and an optional error every statement raises."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.nodes: dict[str, dict[str, dict[str, Any]]] = {
            label: {}
            for label in (
                "CgRevision",
                "CgModule",
                "CgSymbol",
                "CgShape",
                "CgExemplar",
            )
        }
        self.edges: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self.fail = fail
        self.closed = False

    def revisions(self, repo: str) -> list[dict[str, Any]]:
        return sorted(
            (
                r
                for r in self.nodes["CgRevision"].values()
                if r["repo"] == repo and r.get("complete")
            ),
            key=lambda r: r["built_seq"],
        )

    def session(self) -> FakeBoltSession:
        return FakeBoltSession(self)

    def close(self) -> None:
        self.closed = True
