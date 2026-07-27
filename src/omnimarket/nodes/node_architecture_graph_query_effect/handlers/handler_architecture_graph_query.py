# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for architecture graph query operations via Memgraph.

Queries Memgraph over the Bolt protocol using the neo4j Python driver with
parameterized Cypher (no string interpolation of user-supplied values).
Connection parameters come from contract config, never hardcoded.

Operations:
    dependency_path  — shortest path between two nodes
    blast_radius     — all transitively dependent nodes
    cross_repo_imports — cross-repo import relationships for a repo
    circular_deps    — circular dependency chains within a repo
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from omnimarket.nodes.node_architecture_graph_query_effect.models import (
    ModelArchitectureGraphQueryConfig,
    ModelArchitectureGraphQueryRequestedEvent,
    ModelArchitectureGraphQueryResponseEvent,
    ModelArchQueryGraphEdge,
    ModelArchQueryGraphNode,
)

logger = logging.getLogger(__name__)

__all__ = ["HandlerArchitectureGraphQuery", "ProtocolGraphDriver"]

# ---------------------------------------------------------------------------
# Parameterized Cypher query templates
# All user-supplied values passed as parameters — never interpolated.
#
# OMN-14294: openCypher's ``shortestPath()`` function and list-comprehension
# RETURN expressions (``[n IN nodes(p) | n.name]``) are not implemented by
# Memgraph 2.18 — both raised parse/runtime errors when this handler was
# actually exercised against a live Memgraph instance for the first time (the
# golden-chain suite only ever mocked the driver, so this dialect gap shipped
# undetected). ``_CYPHER_DEPENDENCY_PATH`` and ``_CYPHER_CIRCULAR_DEPS`` use
# Memgraph's ``*BFS``/plain variable-length path syntax instead, and return
# ``nodes(p)``/``relationships(p)`` directly — the driver deserializes those
# into property dicts and (start, type, end) tuples, and node-name extraction
# happens in Python instead of in the query.
#
# ``max_depth`` is the config-declared ``max_path_depth`` bound (never
# previously wired into a query) — Memgraph's ``*BFS`` syntax requires an
# explicit upper bound, so this closes both the dialect gap and the dead
# config field in the same fix. ``max_depth`` is operator config, never
# user-supplied, so string-formatting it into the template does not reopen
# the parameterization guarantee below.
# ---------------------------------------------------------------------------

_CYPHER_DEPENDENCY_PATH_TEMPLATE = (
    "MATCH p = (a {{name: $from_node}})-[:DEPENDS_ON *BFS 1..{max_depth}]->(b {{name: $to_node}}) "
    "RETURN nodes(p) AS path_nodes, relationships(p) AS path_rels, size(p) AS path_length"
)

_CYPHER_BLAST_RADIUS = (
    "MATCH (root {name: $target})<-[:DEPENDS_ON*]-(dependent) "
    "RETURN dependent.name AS dependent_node, "
    "labels(dependent)[0] AS node_type, "
    "dependent.repo AS repo"
)

_CYPHER_CROSS_REPO_IMPORTS = (
    "MATCH (a)-[r:IMPORTS]->(b) "
    "WHERE a.repo = $repo AND b.repo <> $repo "
    "RETURN a.repo AS from_repo, b.repo AS to_repo, "
    "a.name AS from_module, b.name AS to_module, type(r) AS edge_type"
)

_CYPHER_CIRCULAR_DEPS_TEMPLATE = (
    "MATCH p = (a {{repo: $repo}})-[:DEPENDS_ON*1..{max_depth}]->(a) "
    "RETURN nodes(p) AS cycle_nodes, size(p) AS cycle_length "
    "LIMIT 50"
)


# ---------------------------------------------------------------------------
# Narrow structural protocol — keeps this module free of a hard neo4j dep
# and gives mypy enough type info for `.session()` and `.close()`.
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolGraphSession(Protocol):
    """Minimal async graph session interface."""

    async def run(self, query: str, **parameters: Any) -> Any: ...

    async def data(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class ProtocolGraphDriver(Protocol):
    """Minimal async graph driver interface (Bolt-compatible)."""

    def session(self) -> Any: ...

    async def close(self) -> None: ...


@asynccontextmanager
async def _open_session(driver: ProtocolGraphDriver) -> AsyncIterator[Any]:
    """Thin wrapper that works with both real neo4j drivers and mock contexts."""
    ctx = driver.session()
    # Support drivers that return an async context manager directly
    if hasattr(ctx, "__aenter__"):
        async with ctx as session:
            yield session
    else:
        yield ctx


class HandlerArchitectureGraphQuery:
    """Handler for architecture graph queries against Memgraph.

    Uses the neo4j Python driver (compatible with Memgraph's Bolt endpoint).
    The driver is injected or created via initialize(); all Cypher queries
    are parameterized — no user values are interpolated into query strings.

    Supported operations:
        - dependency_path(from_node, to_node): shortest dependency path
        - blast_radius(target): all transitively dependent nodes
        - cross_repo_imports(repo): cross-repo import relationships
        - circular_deps(repo): circular dependency chains
    """

    def __init__(self) -> None:
        self._driver: ProtocolGraphDriver | None = None
        self._config: ModelArchitectureGraphQueryConfig | None = None
        self._initialized: bool = False
        self._init_lock = asyncio.Lock()

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def initialize(
        self,
        *,
        config: ModelArchitectureGraphQueryConfig | None = None,
        driver: ProtocolGraphDriver | None = None,
    ) -> None:
        """Initialize with config and optional pre-built driver (for testing)."""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            self._config = config or ModelArchitectureGraphQueryConfig()

            if driver is not None:
                self._driver = driver
            else:
                # Import here to avoid hard dep at module load time
                try:
                    from neo4j import (
                        AsyncGraphDatabase,  # type: ignore[import-untyped,unused-ignore]
                    )

                    self._driver = AsyncGraphDatabase.driver(
                        self._config.bolt_uri,
                        connection_timeout=self._config.timeout_seconds,
                    )
                except ImportError as exc:
                    raise RuntimeError(
                        "neo4j driver not installed. Add 'neo4j' to dependencies."
                    ) from exc

            self._initialized = True
            logger.info(
                "HandlerArchitectureGraphQuery initialized (backend=%s)",
                self._config.graph_backend,
            )

    async def execute(
        self,
        request: ModelArchitectureGraphQueryRequestedEvent,
    ) -> ModelArchitectureGraphQueryResponseEvent:
        """Execute an architecture graph query request.

        Never raises — all errors are returned in the response envelope.
        """
        if not self._initialized or self._driver is None or self._config is None:
            return ModelArchitectureGraphQueryResponseEvent.from_error(
                query_id=request.query_id,
                operation=request.operation,
                error_message="Handler not initialized",
                correlation_id=request.correlation_id,
            )

        start = time.monotonic()
        config = self._config

        try:
            async with asyncio.timeout(config.timeout_seconds):
                match request.operation:
                    case "dependency_path":
                        return await self._handle_dependency_path(request, start)
                    case "blast_radius":
                        return await self._handle_blast_radius(request, start)
                    case "cross_repo_imports":
                        return await self._handle_cross_repo_imports(request, start)
                    case "circular_deps":
                        return await self._handle_circular_deps(request, start)
                    case _:
                        return ModelArchitectureGraphQueryResponseEvent.from_error(
                            query_id=request.query_id,
                            operation=request.operation,
                            error_message=f"Unknown operation: {request.operation!r}",
                            correlation_id=request.correlation_id,
                        )
        except TimeoutError:
            return ModelArchitectureGraphQueryResponseEvent.from_error(
                query_id=request.query_id,
                operation=request.operation,
                error_message=f"Query timeout after {config.timeout_seconds}s",
                correlation_id=request.correlation_id,
            )
        except Exception as exc:
            logger.exception("Architecture graph query failed: %s", request.operation)
            return ModelArchitectureGraphQueryResponseEvent.from_error(
                query_id=request.query_id,
                operation=request.operation,
                error_message=str(exc),
                correlation_id=request.correlation_id,
            )

    async def _handle_dependency_path(
        self,
        request: ModelArchitectureGraphQueryRequestedEvent,
        start: float,
    ) -> ModelArchitectureGraphQueryResponseEvent:
        if not request.from_node or not request.to_node:
            return ModelArchitectureGraphQueryResponseEvent.from_error(
                query_id=request.query_id,
                operation=request.operation,
                error_message="dependency_path requires from_node and to_node",
                correlation_id=request.correlation_id,
            )

        assert self._driver is not None
        assert self._config is not None
        cypher = _CYPHER_DEPENDENCY_PATH_TEMPLATE.format(
            max_depth=self._config.max_path_depth
        )
        async with _open_session(self._driver) as session:
            result = await session.run(
                cypher,
                from_node=request.from_node,
                to_node=request.to_node,
            )
            rows: list[dict[str, Any]] = await result.data()

        if not rows:
            return ModelArchitectureGraphQueryResponseEvent(
                query_id=request.query_id,
                operation=request.operation,
                status="no_results",
                path_length=None,
                execution_time_ms=(time.monotonic() - start) * 1000,
                correlation_id=request.correlation_id,
            )

        row = rows[0]
        # nodes(p)/relationships(p) deserialize to property dicts and
        # (start_props, type, end_props) tuples — Memgraph doesn't support the
        # list-comprehension RETURN form used to extract names in the query.
        path_nodes_raw: list[dict[str, Any]] = row.get("path_nodes", [])
        path_rels_raw: list[Any] = row.get("path_rels", [])
        path_length: int | None = row.get("path_length")

        nodes = tuple(
            ModelArchQueryGraphNode(name=n["name"], node_type="module")
            for n in path_nodes_raw
            if isinstance(n, dict) and n.get("name")
        )
        edges = tuple(
            ModelArchQueryGraphEdge(
                source=e[0].get("name", "") if isinstance(e[0], dict) else str(e[0]),
                edge_type=e[1],
                target=e[2].get("name", "") if isinstance(e[2], dict) else str(e[2]),
            )
            for e in path_rels_raw
            if len(e) == 3
        )

        return ModelArchitectureGraphQueryResponseEvent(
            query_id=request.query_id,
            operation=request.operation,
            status="success",
            nodes=nodes,
            edges=edges,
            path_length=path_length,
            execution_time_ms=(time.monotonic() - start) * 1000,
            correlation_id=request.correlation_id,
        )

    async def _handle_blast_radius(
        self,
        request: ModelArchitectureGraphQueryRequestedEvent,
        start: float,
    ) -> ModelArchitectureGraphQueryResponseEvent:
        if not request.target:
            return ModelArchitectureGraphQueryResponseEvent.from_error(
                query_id=request.query_id,
                operation=request.operation,
                error_message="blast_radius requires target",
                correlation_id=request.correlation_id,
            )

        assert self._driver is not None
        async with _open_session(self._driver) as session:
            result = await session.run(
                _CYPHER_BLAST_RADIUS,
                target=request.target,
            )
            rows = await result.data()

        nodes = tuple(
            ModelArchQueryGraphNode(
                name=row["dependent_node"],
                node_type=row.get("node_type") or "module",
                repo=row.get("repo"),
            )
            for row in rows
            if row.get("dependent_node")
        )
        edges = tuple(
            ModelArchQueryGraphEdge(
                source=row["dependent_node"],
                target=request.target,
                edge_type="DEPENDS_ON",
            )
            for row in rows
            if row.get("dependent_node")
        )

        return ModelArchitectureGraphQueryResponseEvent(
            query_id=request.query_id,
            operation=request.operation,
            status="success",
            nodes=nodes,
            edges=edges,
            execution_time_ms=(time.monotonic() - start) * 1000,
            correlation_id=request.correlation_id,
        )

    async def _handle_cross_repo_imports(
        self,
        request: ModelArchitectureGraphQueryRequestedEvent,
        start: float,
    ) -> ModelArchitectureGraphQueryResponseEvent:
        if not request.repo:
            return ModelArchitectureGraphQueryResponseEvent.from_error(
                query_id=request.query_id,
                operation=request.operation,
                error_message="cross_repo_imports requires repo",
                correlation_id=request.correlation_id,
            )

        assert self._driver is not None
        async with _open_session(self._driver) as session:
            result = await session.run(
                _CYPHER_CROSS_REPO_IMPORTS,
                repo=request.repo,
            )
            rows = await result.data()

        seen_nodes: set[str] = set()
        nodes_list: list[ModelArchQueryGraphNode] = []
        edges_list: list[ModelArchQueryGraphEdge] = []

        for row in rows:
            from_mod: str = row.get("from_module", "")
            to_mod: str = row.get("to_module", "")
            from_repo: str = row.get("from_repo", "")
            to_repo: str = row.get("to_repo", "")
            edge_type: str = row.get("edge_type", "IMPORTS")

            for name, repo in ((from_mod, from_repo), (to_mod, to_repo)):
                if name and name not in seen_nodes:
                    seen_nodes.add(name)
                    nodes_list.append(
                        ModelArchQueryGraphNode(
                            name=name, node_type="module", repo=repo
                        )
                    )

            if from_mod and to_mod:
                edges_list.append(
                    ModelArchQueryGraphEdge(
                        source=from_mod, target=to_mod, edge_type=edge_type
                    )
                )

        return ModelArchitectureGraphQueryResponseEvent(
            query_id=request.query_id,
            operation=request.operation,
            status="success" if rows else "no_results",
            nodes=tuple(nodes_list),
            edges=tuple(edges_list),
            execution_time_ms=(time.monotonic() - start) * 1000,
            correlation_id=request.correlation_id,
        )

    async def _handle_circular_deps(
        self,
        request: ModelArchitectureGraphQueryRequestedEvent,
        start: float,
    ) -> ModelArchitectureGraphQueryResponseEvent:
        if not request.repo:
            return ModelArchitectureGraphQueryResponseEvent.from_error(
                query_id=request.query_id,
                operation=request.operation,
                error_message="circular_deps requires repo",
                correlation_id=request.correlation_id,
            )

        assert self._driver is not None
        assert self._config is not None
        cypher = _CYPHER_CIRCULAR_DEPS_TEMPLATE.format(
            max_depth=self._config.max_path_depth
        )
        async with _open_session(self._driver) as session:
            result = await session.run(
                cypher,
                repo=request.repo,
            )
            rows = await result.data()

        seen_nodes: set[str] = set()
        nodes_list: list[ModelArchQueryGraphNode] = []
        edges_list: list[ModelArchQueryGraphEdge] = []

        for row in rows:
            # nodes(p) deserializes to a list of property dicts — Memgraph
            # doesn't support the list-comprehension RETURN form used to
            # extract names in the query.
            cycle_nodes_raw: list[dict[str, Any]] = row.get("cycle_nodes", [])
            cycle_nodes: list[str] = [
                n["name"]
                for n in cycle_nodes_raw
                if isinstance(n, dict) and n.get("name")
            ]
            for name in cycle_nodes:
                if name and name not in seen_nodes:
                    seen_nodes.add(name)
                    nodes_list.append(
                        ModelArchQueryGraphNode(
                            name=name, node_type="module", repo=request.repo
                        )
                    )
            # Emit edges for each consecutive pair in the cycle
            for i in range(len(cycle_nodes) - 1):
                edges_list.append(
                    ModelArchQueryGraphEdge(
                        source=cycle_nodes[i],
                        target=cycle_nodes[i + 1],
                        edge_type="DEPENDS_ON",
                    )
                )

        return ModelArchitectureGraphQueryResponseEvent(
            query_id=request.query_id,
            operation=request.operation,
            status="success",
            nodes=tuple(nodes_list),
            edges=tuple(edges_list),
            execution_time_ms=(time.monotonic() - start) * 1000,
            correlation_id=request.correlation_id,
        )

    async def shutdown(self) -> None:
        """Release the graph driver connection."""
        if self._initialized and self._driver is not None:
            try:
                await self._driver.close()
            except Exception as exc:
                logger.warning("Error closing graph driver: %s", exc)
            finally:
                self._driver = None

        self._config = None
        self._initialized = False
        logger.info("HandlerArchitectureGraphQuery shutdown complete")
