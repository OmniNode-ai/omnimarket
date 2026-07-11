# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for architecture graph populate operations via Memgraph.

Builds and maintains the ONEX architecture graph in Memgraph from three sources:

  (a) contract.yaml files — each node becomes an ONEXNode, topics become
      KafkaTopic nodes, sub/pub relationships become SUBSCRIBES_TO/PUBLISHES_TO
      edges. Source authority: "authoritative".

  (b) Python import analysis — cross-repo imports become IMPORTS edges between
      PythonModule nodes. Source authority: "evidence".

  (c) pyproject.toml dependencies — become DEPENDS_ON edges between Repository
      nodes. Source authority: "evidence".

All Cypher statements use MERGE for idempotency. Graph snapshot metadata
(graph_schema_version, graph_snapshot_id, populated_from_commit_set) is
tracked on each run.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable
from uuid import uuid4

import yaml

from omnimarket.analysis.ast_import_scanner import (
    ASTImportScanner,
    _extract_imports,
    _module_name,
)
from omnimarket.nodes.node_architecture_graph_populate_effect.models import (
    ModelArchitectureGraphPopulateConfig,
    ModelArchitectureGraphPopulateRequestedEvent,
    ModelArchitectureGraphPopulateResponseEvent,
    ModelGraphEdgeSpec,
    ModelGraphNodeSpec,
    ModelGraphSnapshotMeta,
)

logger = logging.getLogger(__name__)

__all__ = ["HandlerArchitectureGraphPopulate", "ProtocolGraphDriver"]

# ---------------------------------------------------------------------------
# Node label constants
# ---------------------------------------------------------------------------
_LABEL_REPOSITORY = "Repository"
_LABEL_ONEX_NODE = "ONEXNode"
_LABEL_KAFKA_TOPIC = "KafkaTopic"
_LABEL_PYTHON_MODULE = "PythonModule"

# ---------------------------------------------------------------------------
# Transient-write retry — adapted from omniarchon's
# services/intelligence/storage/memgraph_adapter.py::retry_on_transient_error
# (OMN-14295 harvest). Memgraph raises TransientError on conflicting/
# serialization conflicts even for sequential writes when another process
# touches the graph concurrently — realistic once post-merge incremental
# populate runs overlap with a manual one. Exponential backoff, only retries
# errors that look transient; everything else re-raises immediately.
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


def _retry_on_transient_error(
    max_attempts: int = 3,
    initial_backoff: float = 0.1,
    backoff_multiplier: float = 2.0,
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    def decorator(
        func: Callable[..., Awaitable[_T]],
    ) -> Callable[..., Awaitable[_T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _T:
            attempt = 0
            backoff = initial_backoff
            while True:
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    error_msg = str(exc).lower()
                    is_transient = (
                        "transienterror" in error_msg
                        or "transient" in error_msg
                        or "conflicting transactions" in error_msg
                        or "serialization" in error_msg
                        or "transaction conflict" in error_msg
                    )
                    attempt += 1
                    if not is_transient or attempt >= max_attempts:
                        raise
                    logger.warning(
                        "Memgraph write retry %d/%d for %s: transient error %r",
                        attempt,
                        max_attempts,
                        func.__name__,
                        str(exc)[:200],
                    )
                    await asyncio.sleep(backoff)
                    backoff *= backoff_multiplier

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Narrow structural protocol — keeps hard neo4j dep lazy
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolGraphSession(Protocol):
    """Minimal async graph session interface."""

    async def run(self, query: str, **parameters: Any) -> Any: ...

    async def data(self) -> list[dict[str, Any]]: ...

    async def consume(self) -> Any: ...


@runtime_checkable
class ProtocolGraphDriver(Protocol):
    """Minimal async graph driver interface (Bolt-compatible)."""

    def session(self) -> Any: ...

    async def close(self) -> None: ...


@asynccontextmanager
async def _open_session(driver: ProtocolGraphDriver) -> AsyncIterator[Any]:
    ctx = driver.session()
    if hasattr(ctx, "__aenter__"):
        async with ctx as session:
            yield session
    else:
        yield ctx


class HandlerArchitectureGraphPopulate:
    """Handler for populating the architecture graph in Memgraph.

    Sources:
      - contract.yaml files (authoritative)
      - Python import analysis (evidence)
      - pyproject.toml dependencies (evidence)

    All Cypher uses MERGE — runs are fully idempotent.
    """

    def __init__(self) -> None:
        self._driver: ProtocolGraphDriver | None = None
        self._config: ModelArchitectureGraphPopulateConfig | None = None
        self._initialized: bool = False
        self._init_lock = asyncio.Lock()

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def initialize(
        self,
        *,
        config: ModelArchitectureGraphPopulateConfig | None = None,
        driver: ProtocolGraphDriver | None = None,
    ) -> None:
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            self._config = config or ModelArchitectureGraphPopulateConfig()

            if driver is not None:
                self._driver = driver
            else:
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
                "HandlerArchitectureGraphPopulate initialized (backend=%s)",
                self._config.graph_backend,
            )

    async def execute(
        self,
        request: ModelArchitectureGraphPopulateRequestedEvent,
    ) -> ModelArchitectureGraphPopulateResponseEvent:
        """Execute a graph populate request. Never raises — errors in response envelope."""
        if not self._initialized or self._driver is None or self._config is None:
            return ModelArchitectureGraphPopulateResponseEvent.from_error(
                populate_id=request.populate_id,
                operation=request.operation,
                error_message="Handler not initialized",
                correlation_id=request.correlation_id,
            )

        start = time.monotonic()
        config = self._config

        try:
            async with asyncio.timeout(config.timeout_seconds):
                return await self._handle_populate(request, start)
        except TimeoutError:
            return ModelArchitectureGraphPopulateResponseEvent.from_error(
                populate_id=request.populate_id,
                operation=request.operation,
                error_message=f"Populate timeout after {config.timeout_seconds}s",
                correlation_id=request.correlation_id,
            )
        except Exception as exc:
            logger.exception(
                "Architecture graph populate failed: %s", request.operation
            )
            return ModelArchitectureGraphPopulateResponseEvent.from_error(
                populate_id=request.populate_id,
                operation=request.operation,
                error_message=str(exc),
                correlation_id=request.correlation_id,
            )

    async def _handle_populate(
        self,
        request: ModelArchitectureGraphPopulateRequestedEvent,
        start: float,
    ) -> ModelArchitectureGraphPopulateResponseEvent:
        omni_home = Path(request.omni_home)
        repo_filter = set(request.repos) if request.repos else None

        all_nodes: list[ModelGraphNodeSpec] = []
        all_edges: list[ModelGraphEdgeSpec] = []
        commit_set: list[str] = []

        # Discover repos
        repos = self._discover_repos(omni_home, repo_filter)

        for repo_path in repos:
            repo_name = repo_path.name

            # Repo node — always created. "repo" (not just "name") is required
            # so cross_repo_imports can filter targets by repo (OMN-14295):
            # IMPORTS edges target a Repository node, and the query's
            # `b.repo <> $repo` clause silently drops every row when that
            # property is unset (Cypher NULL comparisons are neither true
            # nor false, so the WHERE clause filters the row out).
            all_nodes.append(
                ModelGraphNodeSpec(
                    node_id=repo_name,
                    label=_LABEL_REPOSITORY,
                    properties={
                        "name": repo_name,
                        "repo": repo_name,
                        "path": str(repo_path),
                    },
                )
            )

            # Collect HEAD commit if available
            head_file = repo_path / ".git" / "HEAD"
            if head_file.exists():
                try:
                    ref_line = head_file.read_text().strip()
                    if ref_line.startswith("ref: "):
                        ref_path = repo_path / ".git" / ref_line[5:]
                        if ref_path.exists():
                            commit_set.append(ref_path.read_text().strip()[:12])
                    else:
                        commit_set.append(ref_line[:12])
                except OSError:
                    pass

            op = request.operation
            if op in ("populate_from_contracts", "populate_all"):
                nodes, edges = self._collect_contract_data(repo_path, repo_name)
                all_nodes.extend(nodes)
                all_edges.extend(edges)

            if op in ("populate_from_imports", "populate_all"):
                nodes, edges = self._collect_import_edges(repo_path, repo_name)
                all_nodes.extend(nodes)
                all_edges.extend(edges)

            if op in ("populate_from_pyproject", "populate_all"):
                pyproject_path = repo_path / "pyproject.toml"
                if pyproject_path.exists():
                    with open(pyproject_path, "rb") as f:
                        pyproject_data = tomllib.load(f)
                    edges = self._parse_pyproject_deps(
                        repo=repo_name, pyproject=pyproject_data
                    )
                    all_edges.extend(edges)

        # Deduplicate nodes by node_id
        seen_node_ids: set[str] = set()
        deduped_nodes: list[ModelGraphNodeSpec] = []
        for n in all_nodes:
            if n.node_id not in seen_node_ids:
                seen_node_ids.add(n.node_id)
                deduped_nodes.append(n)

        # Deduplicate edges
        seen_edge_keys: set[tuple[str, str, str]] = set()
        deduped_edges: list[ModelGraphEdgeSpec] = []
        for e in all_edges:
            key = (e.source_id, e.edge_type, e.target_id)
            if key not in seen_edge_keys:
                seen_edge_keys.add(key)
                deduped_edges.append(e)

        snapshot_id = str(uuid4())
        if self._config is None:
            raise RuntimeError(
                "HandlerArchitectureGraphPopulate.handle() called before initialize(); "
                "call initialize() first so the overlay-resolved config is available"
            )
        schema_version = self._config.graph_schema_version

        snapshot_meta = ModelGraphSnapshotMeta(
            graph_schema_version=schema_version,
            graph_snapshot_id=snapshot_id,
            populated_from_commit_set=tuple(commit_set),
            repo_count=len(repos),
            node_count=len(deduped_nodes),
            edge_count=len(deduped_edges),
        )

        if not request.dry_run:
            await self._write_to_graph(deduped_nodes, deduped_edges, snapshot_meta)

        status: Literal["dry_run", "success"] = (
            "dry_run" if request.dry_run else "success"
        )
        return ModelArchitectureGraphPopulateResponseEvent(
            populate_id=request.populate_id,
            operation=request.operation,
            status=status,
            snapshot_meta=snapshot_meta,
            nodes_written=tuple(deduped_nodes),
            edges_written=tuple(deduped_edges),
            execution_time_ms=(time.monotonic() - start) * 1000,
            correlation_id=request.correlation_id,
        )

    def _discover_repos(
        self, omni_home: Path, repo_filter: set[str] | None
    ) -> list[Path]:
        """Find all git repos directly under omni_home (one level deep)."""
        repos: list[Path] = []
        if not omni_home.exists():
            return repos
        for child in sorted(omni_home.iterdir()):
            if not child.is_dir():
                continue
            if repo_filter and child.name not in repo_filter:
                continue
            if (child / ".git").exists():
                repos.append(child)
        return repos

    def _collect_contract_data(
        self, repo_path: Path, repo_name: str
    ) -> tuple[list[ModelGraphNodeSpec], list[ModelGraphEdgeSpec]]:
        """Walk repo for contract.yaml files; parse each into node/edge specs."""
        nodes: list[ModelGraphNodeSpec] = []
        edges: list[ModelGraphEdgeSpec] = []

        for contract_path in repo_path.rglob("contract.yaml"):
            try:
                with open(contract_path) as f:
                    contract = yaml.safe_load(f)
                if not isinstance(contract, dict):
                    continue
                node_name = contract.get("name", "")
                if not node_name:
                    continue
                n, e = self._parse_contract_data(
                    repo=repo_name, node_name=node_name, contract=contract
                )
                nodes.extend(n)
                edges.extend(e)
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", contract_path, exc)

        return nodes, edges

    def _parse_contract_data(
        self, *, repo: str, node_name: str, contract: dict[str, Any]
    ) -> tuple[list[ModelGraphNodeSpec], list[ModelGraphEdgeSpec]]:
        """Parse a single contract dict into node and edge specs."""
        nodes: list[ModelGraphNodeSpec] = []
        edges: list[ModelGraphEdgeSpec] = []

        node_type = contract.get("node_type", "unknown")

        # ONEXNode for this node
        onex_node = ModelGraphNodeSpec(
            node_id=node_name,
            label=_LABEL_ONEX_NODE,
            properties={
                "name": node_name,
                "repo": repo,
                "node_type": str(node_type),
            },
        )
        nodes.append(onex_node)

        # CONTAINS edge: Repository -> ONEXNode
        edges.append(
            ModelGraphEdgeSpec(
                source_id=repo,
                target_id=node_name,
                edge_type="CONTAINS",
                source_authority="authoritative",
            )
        )

        # Parse event_bus sub/pub topics
        event_bus = contract.get("event_bus", {})
        if isinstance(event_bus, dict):
            for topic in event_bus.get("subscribe_topics", []):
                if not isinstance(topic, str):
                    continue
                nodes.append(
                    ModelGraphNodeSpec(
                        node_id=topic,
                        label=_LABEL_KAFKA_TOPIC,
                        properties={"name": topic},
                    )
                )
                edges.append(
                    ModelGraphEdgeSpec(
                        source_id=node_name,
                        target_id=topic,
                        edge_type="SUBSCRIBES_TO",
                        source_authority="authoritative",
                    )
                )
            for topic in event_bus.get("publish_topics", []):
                if not isinstance(topic, str):
                    continue
                nodes.append(
                    ModelGraphNodeSpec(
                        node_id=topic,
                        label=_LABEL_KAFKA_TOPIC,
                        properties={"name": topic},
                    )
                )
                edges.append(
                    ModelGraphEdgeSpec(
                        source_id=node_name,
                        target_id=topic,
                        edge_type="PUBLISHES_TO",
                        source_authority="authoritative",
                    )
                )

        return nodes, edges

    # Known ONEX repos for cross-repo import detection
    _KNOWN_REPOS = frozenset(
        [
            "omnibase_core",
            "omnibase_infra",
            "omnibase_spi",
            "omnibase_compat",
            "omnimarket",
            "omniclaude",
            "omnimemory",
            "omniintelligence",
            "omninode_infra",
            "onex_change_control",
            "omnidash",
        ]
    )

    def _collect_import_edges(
        self, repo_path: Path, repo_name: str
    ) -> tuple[list[ModelGraphNodeSpec], list[ModelGraphEdgeSpec]]:
        """PythonModule nodes + cross-repo IMPORTS edges for repo_path.

        OMN-14295: reuses the shared ``ASTImportScanner`` (exact
        filesystem-path resolution, OMN-11046, 13 passing tests) instead of a
        duplicate coarse ast.walk — the module_id for both the PythonModule
        node and every edge's source_id is derived by the SAME
        ``_module_name`` helper the scanner uses internally for its own
        module identity, so an IMPORTS edge can never reference a node this
        method didn't also create (the "one canonical id-builder" fix — the
        prior version's edge MERGE ``MATCH (a {node_id: ...})`` found zero
        rows because nothing else ever created that node_id).

        ``ASTImportScanner.scan()`` only returns edges it can resolve to a
        real file *within* the scanned root, so it can't see cross-repo
        imports on its own — those are still detected here via the shared,
        tested ``_extract_imports`` helper against the ``_KNOWN_REPOS`` set,
        but every PythonModule node this repo could need is created from
        ``graph.nodes`` regardless of whether that file happens to import
        cross-repo.
        """
        src_root = repo_path / "src"
        if not src_root.exists():
            src_root = repo_path

        graph = ASTImportScanner().scan(src_root)

        nodes: list[ModelGraphNodeSpec] = []
        edges: list[ModelGraphEdgeSpec] = []

        for rel_path in graph.nodes:
            abs_path = src_root / rel_path
            dotted = _module_name(abs_path, src_root)
            module_id = f"{repo_name}::{dotted}"
            nodes.append(
                ModelGraphNodeSpec(
                    node_id=module_id,
                    label=_LABEL_PYTHON_MODULE,
                    properties={
                        "name": module_id,
                        "repo": repo_name,
                        "rel_path": rel_path,
                    },
                )
            )

            try:
                source = abs_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for imported_module in _extract_imports(source):
                top_pkg = imported_module.split(".")[0]
                if top_pkg in self._KNOWN_REPOS and top_pkg != repo_name:
                    edges.append(
                        ModelGraphEdgeSpec(
                            source_id=module_id,
                            target_id=top_pkg,
                            edge_type="IMPORTS",
                            source_authority="evidence",
                            properties={
                                "imported_module": imported_module,
                                "source_repo": repo_name,
                            },
                        )
                    )

        return nodes, edges

    def _parse_pyproject_deps(
        self, *, repo: str, pyproject: dict[str, Any]
    ) -> list[ModelGraphEdgeSpec]:
        """Extract DEPENDS_ON edges from pyproject.toml project.dependencies."""
        edges: list[ModelGraphEdgeSpec] = []
        project = pyproject.get("project", {})
        if not isinstance(project, dict):
            return edges

        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list):
            return edges

        for dep in dependencies:
            if not isinstance(dep, str):
                continue
            # Extract package name before any version specifier
            pkg_name = re.split(r"[>=<!;\[]", dep)[0].strip().replace("-", "_")
            if not pkg_name:
                continue
            edges.append(
                ModelGraphEdgeSpec(
                    source_id=repo,
                    target_id=pkg_name,
                    edge_type="DEPENDS_ON",
                    source_authority="evidence",
                    properties={"raw_dep": dep},
                )
            )

        return edges

    def _build_node_batch_cypher(self, label: str) -> str:
        """Build a parameterized, UNWIND-batched MERGE statement for a batch
        of graph nodes sharing ``label``.

        OMN-14295: the label must be a literal (Cypher doesn't allow
        parameterizing a node label), so nodes are grouped by label and one
        UNWIND MERGE runs per batch instead of one MERGE per node. ``SET n +=
        row.properties`` merges whatever property keys each row happens to
        carry — nodes of the same label already share a property shape, but
        this doesn't require it.
        """
        return (
            "UNWIND $rows AS row "
            f"MERGE (n:{label} {{node_id: row.node_id}}) "
            "SET n += row.properties"
        )

    def _build_edge_batch_cypher(self, edge_type: str) -> str:
        """Build a parameterized, UNWIND-batched MERGE statement for a batch
        of graph edges sharing ``edge_type`` (relationship type is also a
        Cypher literal, not parameterizable)."""
        return (
            "UNWIND $rows AS row "
            "MATCH (a {node_id: row.source_id}), (b {node_id: row.target_id}) "
            f"MERGE (a)-[r:{edge_type}]->(b) "
            "SET r += row.properties, r.source_authority = row.source_authority"
        )

    @_retry_on_transient_error()
    async def _run_batch(self, session: Any, cypher: str, **params: Any) -> None:
        """Run one batched write and consume its summary, retrying on a
        Memgraph TransientError (OMN-14295 harvest from omniarchon's
        retry_on_transient_error — see module-level comment)."""
        result = await session.run(cypher, **params)
        await result.consume()

    async def _write_to_graph(
        self,
        nodes: list[ModelGraphNodeSpec],
        edges: list[ModelGraphEdgeSpec],
        snapshot_meta: ModelGraphSnapshotMeta,
    ) -> None:
        """Execute MERGE statements against Memgraph in UNWIND-batched writes.

        OMN-14295: one round trip per node/edge (the prior behavior) measured
        ~300ms/call over a real network hop to Memgraph — a full-registry
        populate never completed within any reasonable timeout. Grouping by
        label/edge_type and sending ``batch_size`` rows per UNWIND MERGE cuts
        a ~1500-item populate from ~450s to single-digit seconds.
        """
        assert self._driver is not None
        if self._config is None:
            raise RuntimeError(
                "HandlerArchitectureGraphPopulate._write_to_graph() called before "
                "initialize(); the overlay-resolved config must be set"
            )
        batch_size = self._config.batch_size

        nodes_by_label: dict[str, list[ModelGraphNodeSpec]] = {}
        for node in nodes:
            nodes_by_label.setdefault(node.label, []).append(node)

        edges_by_type: dict[str, list[ModelGraphEdgeSpec]] = {}
        for edge in edges:
            edges_by_type.setdefault(edge.edge_type, []).append(edge)

        async with _open_session(self._driver) as session:
            for label, label_nodes in nodes_by_label.items():
                cypher = self._build_node_batch_cypher(label)
                for i in range(0, len(label_nodes), batch_size):
                    chunk = label_nodes[i : i + batch_size]
                    rows = [
                        {"node_id": n.node_id, "properties": n.properties}
                        for n in chunk
                    ]
                    await self._run_batch(session, cypher, rows=rows)

            for edge_type, type_edges in edges_by_type.items():
                cypher = self._build_edge_batch_cypher(edge_type)
                for i in range(0, len(type_edges), batch_size):
                    edge_chunk = type_edges[i : i + batch_size]
                    rows = [
                        {
                            "source_id": e.source_id,
                            "target_id": e.target_id,
                            "properties": e.properties,
                            "source_authority": e.source_authority,
                        }
                        for e in edge_chunk
                    ]
                    await self._run_batch(session, cypher, rows=rows)

            # Stamp snapshot metadata as a SnapshotMeta node
            await self._run_batch(
                session,
                "MERGE (s:GraphSnapshot {snapshot_id: $snapshot_id}) "
                "SET s.schema_version = $schema_version, "
                "    s.repo_count = $repo_count, "
                "    s.node_count = $node_count, "
                "    s.edge_count = $edge_count",
                snapshot_id=snapshot_meta.graph_snapshot_id,
                schema_version=snapshot_meta.graph_schema_version,
                repo_count=snapshot_meta.repo_count,
                node_count=snapshot_meta.node_count,
                edge_count=snapshot_meta.edge_count,
            )

        logger.info(
            "Graph populate complete: %d nodes, %d edges (snapshot=%s)",
            snapshot_meta.node_count,
            snapshot_meta.edge_count,
            snapshot_meta.graph_snapshot_id,
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
        logger.info("HandlerArchitectureGraphPopulate shutdown complete")
