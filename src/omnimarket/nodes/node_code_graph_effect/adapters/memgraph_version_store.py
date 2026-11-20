# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Memgraph-backed version store of the code graph lookup (OMN-26104).

One stored version is one revision of one repository: a ``CgRevision`` node keyed ``<repo>@<commit>``,
a ``CgModule`` node per module file, a ``CgSymbol`` node per class or function, ``CG_IMPORTS`` edges
between modules, ``CG_DEFINES`` edges from a module to its symbols, and ``CgShape`` and ``CgExemplar``
nodes for the same-shape and exemplar questions. Every node and edge carries the revision key in
``rev``, so the counts of one build read back with ``MATCH (n {rev: $key})`` and a prune is a delete by
``rev``. Every write is a ``MERGE``; a revision is readable only once its ``complete`` flag is set, the
last statement of a write, so a write that died halfway is rewritten by the next build and never read.

The driver is injected (``ProtocolBoltDriver``), so the unit tests run a fake Bolt session with no
network. A transport failure is a typed :class:`MemgraphUnreachableError` carrying an
:class:`EnumLookupFallbackReason`; a query Memgraph refuses is a :class:`MemgraphQueryError`, which is
never a fallback.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, cast

from omnimarket.models.model_code_graph_lookup import (
    EnumLookupFallbackReason,
    ModelCodeGraphVersion,
    ModelExemplarNode,
    ModelImportRelation,
    ModelShapeEntry,
    ModelSymbolDefinition,
)

#: Rows sent per ``UNWIND`` statement.
BATCH_SIZE = 1000
#: Versions of a repository kept when the caller names no retention.
DEFAULT_RETENTION_VERSIONS = 7
#: Seconds the driver waits to open the Bolt connection.
CONNECTION_TIMEOUT_SECONDS = 5.0

_NODE_LABELS = ("CgSymbol", "CgShape", "CgExemplar", "CgModule", "CgRevision")
_INDEXES = (
    *(f"CREATE INDEX ON :{label}(rev)" for label in _NODE_LABELS),
    *(f"CREATE INDEX ON :{label}(key)" for label in _NODE_LABELS),
    "CREATE INDEX ON :CgRevision(repo)",
)


class MemgraphUnreachableError(RuntimeError):
    """Memgraph could not be reached or did not answer: the one case the lookup falls back on."""

    def __init__(self, reason: EnumLookupFallbackReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


class MemgraphQueryError(RuntimeError):
    """Memgraph answered and refused the statement. Never a fallback: the answer is a typed failure."""


class ProtocolBoltSession(Protocol):
    def run(self, query: str, parameters: Mapping[str, Any] | None = None) -> Any: ...

    def close(self) -> None: ...


class ProtocolBoltDriver(Protocol):
    def session(self) -> ProtocolBoltSession: ...

    def close(self) -> None: ...


def unreachable_reason(exc: BaseException) -> EnumLookupFallbackReason | None:
    """The typed reason a transport error maps to, or none when the error is not a transport error."""
    from neo4j.exceptions import (
        ConnectionAcquisitionTimeoutError,
        ServiceUnavailable,
        SessionExpired,
    )

    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, ConnectionRefusedError) for item in chain):
        return EnumLookupFallbackReason.CONNECTION_REFUSED
    if any(
        isinstance(item, TimeoutError | ConnectionAcquisitionTimeoutError)
        for item in chain
    ):
        return EnumLookupFallbackReason.READ_TIMED_OUT
    if any(
        isinstance(item, OSError | ServiceUnavailable | SessionExpired)
        for item in chain
    ):
        return EnumLookupFallbackReason.UNREACHABLE
    return None


def _chunks(rows: Sequence[Mapping[str, Any]]) -> Iterator[list[Mapping[str, Any]]]:
    for start in range(0, len(rows), BATCH_SIZE):
        yield list(rows[start : start + BATCH_SIZE])


def revision_key(repo: str, commit: str) -> str:
    """The revision label of one build of one repository."""
    return f"{repo}@{commit}"


class MemgraphVersionStore:
    """``ProtocolCodeGraphVersionStore`` and ``ProtocolCommitClock`` over a Bolt driver."""

    def __init__(
        self,
        driver: ProtocolBoltDriver,
        *,
        retention_versions: int = DEFAULT_RETENTION_VERSIONS,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if retention_versions < 1:
            raise ValueError("retention_versions is at least 1")
        self._driver = driver
        self._retention = retention_versions
        self._clock_ns = clock_ns
        self._schema_ready = False

    def close(self) -> None:
        self._driver.close()

    # -- transport -----------------------------------------------------------------------------

    def _run(self, query: str, **parameters: Any) -> list[dict[str, Any]]:
        try:
            session = self._driver.session()
            try:
                result = session.run(query, parameters)
                return [dict(row) for row in result]
            finally:
                session.close()
        except MemgraphUnreachableError:
            raise
        except Exception as exc:
            reason = unreachable_reason(exc)
            if reason is not None:
                raise MemgraphUnreachableError(
                    reason, f"{type(exc).__name__}: {exc}"
                ) from exc
            raise MemgraphQueryError(f"{type(exc).__name__}: {exc}") from exc

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        for statement in _INDEXES:
            try:
                self._run(statement)
            except MemgraphQueryError as exc:
                if "already exists" not in str(exc):
                    raise
        self._schema_ready = True

    # -- the version store port ----------------------------------------------------------------

    def put(self, version: ModelCodeGraphVersion) -> tuple[bool, tuple[str, ...]]:
        """Write the version by ``MERGE``; ``(written, pruned)`` like the in-memory store."""
        key = revision_key(version.repo, version.commit)
        self._ensure_schema()
        if self._run(
            "MATCH (r:CgRevision {key: $key, complete: true}) RETURN r.key AS key",
            key=key,
        ):
            return False, ()
        self._run(
            "MERGE (r:CgRevision {key: $key}) "
            "ON CREATE SET r.built_seq = $seq "
            "SET r.repo = $repo, r.commit = $commit, r.rev = $key, r.committed_at = $committed_at",
            key=key,
            seq=self._clock_ns(),
            repo=version.repo,
            commit=version.commit,
            committed_at=version.committed_at.isoformat()
            if version.committed_at
            else None,
        )
        paths = sorted(
            {d.path for d in version.definitions}
            | {s.path for s in version.shapes}
            | {i.importer_path for i in version.imports}
            | {i.imported_path for i in version.imports}
        )
        self._write_modules(key, version, paths)
        self._write_symbols(key, version)
        self._write_imports(key, version)
        self._write_shapes(key, version)
        self._write_exemplars(key, version)
        self._run(
            "MATCH (r:CgRevision {key: $key}) SET r.complete = true, "
            "r.modules = $modules, r.symbols = $symbols, r.imports = $imports",
            key=key,
            modules=len(paths),
            symbols=len(version.definitions),
            imports=len(version.imports),
        )
        return True, self._prune(version.repo)

    def _write_modules(
        self, key: str, version: ModelCodeGraphVersion, paths: Iterable[str]
    ) -> None:
        rows = [{"key": f"{key}|{path}", "path": path} for path in paths]
        for batch in _chunks(rows):
            self._run(
                "UNWIND $rows AS row MATCH (r:CgRevision {key: $rev}) "
                "MERGE (m:CgModule {key: row.key}) "
                "SET m.repo = $repo, m.commit = $commit, m.path = row.path, m.rev = $rev "
                "MERGE (r)-[c:CG_CONTAINS]->(m) SET c.rev = $rev",
                rows=batch,
                rev=key,
                repo=version.repo,
                commit=version.commit,
            )

    def _write_symbols(self, key: str, version: ModelCodeGraphVersion) -> None:
        rows = [
            {
                "key": f"{key}|{d.path}|{d.kind}|{d.name}|{d.line}",
                "module": f"{key}|{d.path}",
                "name": d.name,
                "kind": d.kind,
                "path": d.path,
                "line": d.line,
            }
            for d in version.definitions
        ]
        for batch in _chunks(rows):
            self._run(
                "UNWIND $rows AS row MATCH (m:CgModule {key: row.module}) "
                "MERGE (s:CgSymbol {key: row.key}) "
                "SET s.name = row.name, s.kind = row.kind, s.path = row.path, "
                "s.line = row.line, s.rev = $rev "
                "MERGE (m)-[d:CG_DEFINES]->(s) SET d.rev = $rev",
                rows=batch,
                rev=key,
            )

    def _write_imports(self, key: str, version: ModelCodeGraphVersion) -> None:
        rows = [
            {
                "source": f"{key}|{i.importer_path}",
                "target": f"{key}|{i.imported_path}",
            }
            for i in version.imports
        ]
        for batch in _chunks(rows):
            self._run(
                "UNWIND $rows AS row "
                "MATCH (a:CgModule {key: row.source}), (b:CgModule {key: row.target}) "
                "MERGE (a)-[i:CG_IMPORTS]->(b) SET i.rev = $rev",
                rows=batch,
                rev=key,
            )

    def _write_shapes(self, key: str, version: ModelCodeGraphVersion) -> None:
        rows = [
            {
                "key": f"{key}|{s.path}|{s.name}|{s.line}",
                "module": f"{key}|{s.path}",
                "name": s.name,
                "path": s.path,
                "line": s.line,
                "signature": s.signature,
                "fields": list(s.fields),
            }
            for s in version.shapes
        ]
        for batch in _chunks(rows):
            self._run(
                "UNWIND $rows AS row MATCH (m:CgModule {key: row.module}) "
                "MERGE (h:CgShape {key: row.key}) "
                "SET h.name = row.name, h.path = row.path, h.line = row.line, "
                "h.signature = row.signature, h.fields = row.fields, h.rev = $rev "
                "MERGE (m)-[d:CG_DEFINES_SHAPE]->(h) SET d.rev = $rev",
                rows=batch,
                rev=key,
            )

    def _write_exemplars(self, key: str, version: ModelCodeGraphVersion) -> None:
        rows = [
            {
                "key": f"{key}|{e.path}",
                "path": e.path,
                "name": e.name,
                "archetype": e.archetype,
            }
            for e in version.exemplars
        ]
        for batch in _chunks(rows):
            self._run(
                "UNWIND $rows AS row MATCH (r:CgRevision {key: $rev}) "
                "MERGE (e:CgExemplar {key: row.key}) "
                "SET e.path = row.path, e.name = row.name, e.archetype = row.archetype, e.rev = $rev "
                "MERGE (r)-[c:CG_HAS_EXEMPLAR]->(e) SET c.rev = $rev",
                rows=batch,
                rev=key,
            )

    def _prune(self, repo: str) -> tuple[str, ...]:
        rows = self._run(
            "MATCH (r:CgRevision {repo: $repo, complete: true}) "
            "RETURN r.key AS key, r.commit AS commit ORDER BY r.built_seq",
            repo=repo,
        )
        dropped = rows[: -self._retention] if len(rows) > self._retention else []
        for row in dropped:
            for label in _NODE_LABELS:
                self._run(
                    f"MATCH (n:{label} {{rev: $rev}}) DETACH DELETE n", rev=row["key"]
                )
        return tuple(str(row["commit"]) for row in dropped)

    def get(self, repo: str, commit: str) -> ModelCodeGraphVersion | None:
        return self._load(revision_key(repo, commit))

    def newest(self, repo: str) -> ModelCodeGraphVersion | None:
        rows = self._run(
            "MATCH (r:CgRevision {repo: $repo, complete: true}) "
            "RETURN r.key AS key ORDER BY r.built_seq DESC LIMIT 1",
            repo=repo,
        )
        return self._load(str(rows[0]["key"])) if rows else None

    def versions(self, repo: str) -> tuple[ModelCodeGraphVersion, ...]:
        """Every stored revision, oldest first, as its commit and time only (the nearest-version read)."""
        rows = self._run(
            "MATCH (r:CgRevision {repo: $repo, complete: true}) "
            "RETURN r.commit AS commit, r.committed_at AS committed_at ORDER BY r.built_seq",
            repo=repo,
        )
        return tuple(
            ModelCodeGraphVersion(
                repo=repo,
                commit=str(row["commit"]),
                committed_at=_parse_time(row["committed_at"]),
            )
            for row in rows
        )

    def committed_at(self, repo: str, commit: str) -> datetime | None:
        """The commit clock of the lookup: the time recorded on a stored revision, none for any other commit."""
        rows = self._run(
            "MATCH (r:CgRevision {key: $key, complete: true}) RETURN r.committed_at AS committed_at",
            key=revision_key(repo, commit),
        )
        return _parse_time(rows[0]["committed_at"]) if rows else None

    def _load(self, key: str) -> ModelCodeGraphVersion | None:
        head = self._run(
            "MATCH (r:CgRevision {key: $key, complete: true}) "
            "RETURN r.repo AS repo, r.commit AS commit, r.committed_at AS committed_at",
            key=key,
        )
        if not head:
            return None
        definitions = self._run(
            "MATCH (s:CgSymbol {rev: $key}) "
            "RETURN s.name AS name, s.kind AS kind, s.path AS path, s.line AS line",
            key=key,
        )
        imports = self._run(
            "MATCH (a:CgModule {rev: $key})-[:CG_IMPORTS]->(b:CgModule) "
            "RETURN a.path AS importer, b.path AS imported",
            key=key,
        )
        shapes = self._run(
            "MATCH (h:CgShape {rev: $key}) "
            "RETURN h.name AS name, h.path AS path, h.line AS line, "
            "h.signature AS signature, h.fields AS fields",
            key=key,
        )
        exemplars = self._run(
            "MATCH (e:CgExemplar {rev: $key}) "
            "RETURN e.path AS path, e.name AS name, e.archetype AS archetype",
            key=key,
        )
        return ModelCodeGraphVersion(
            repo=str(head[0]["repo"]),
            commit=str(head[0]["commit"]),
            committed_at=_parse_time(head[0]["committed_at"]),
            definitions=tuple(
                ModelSymbolDefinition(
                    name=row["name"],
                    kind=row["kind"],
                    path=row["path"],
                    line=row["line"],
                )
                for row in definitions
            ),
            shapes=tuple(
                ModelShapeEntry(
                    name=row["name"],
                    path=row["path"],
                    line=row["line"],
                    signature=row["signature"],
                    fields=tuple(row["fields"]),
                )
                for row in shapes
            ),
            imports=tuple(
                ModelImportRelation(
                    importer_path=row["importer"], imported_path=row["imported"]
                )
                for row in imports
            ),
            exemplars=tuple(
                ModelExemplarNode(
                    path=row["path"], name=row["name"], archetype=row["archetype"]
                )
                for row in exemplars
            ),
        )


def _parse_time(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def default_driver(bolt_uri: str) -> ProtocolBoltDriver:
    """The neo4j driver for a Bolt URI, no auth (the dev instance takes none)."""
    from neo4j import GraphDatabase

    return cast(
        "ProtocolBoltDriver",
        GraphDatabase.driver(
            bolt_uri, auth=None, connection_timeout=CONNECTION_TIMEOUT_SECONDS
        ),
    )


def open_memgraph_store(
    *,
    resolve_uri: Callable[[], str],
    driver_factory: Callable[[str], ProtocolBoltDriver] = default_driver,
    retention_versions: int = DEFAULT_RETENTION_VERSIONS,
) -> MemgraphVersionStore:
    """The store over the Bolt URI the overlay (or ``ARCH_GRAPH_BOLT_URI``) resolves.

    An unresolved URI is :class:`MemgraphUnreachableError` with ``BOLT_URI_UNRESOLVED``; a refused or
    unreachable port surfaces on the store's first statement, with its own reason.
    """
    try:
        uri = resolve_uri()
    except ValueError as exc:
        raise MemgraphUnreachableError(
            EnumLookupFallbackReason.BOLT_URI_UNRESOLVED, str(exc)
        ) from exc
    try:
        driver = driver_factory(uri)
    except Exception as exc:
        reason = unreachable_reason(exc)
        if reason is None:
            raise MemgraphQueryError(f"{type(exc).__name__}: {exc}") from exc
        raise MemgraphUnreachableError(reason, f"{type(exc).__name__}: {exc}") from exc
    return MemgraphVersionStore(driver, retention_versions=retention_versions)
