# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lookup: five questions answered from one stored version of a repository's graph (OMN-23648).

Asked at the head, a lookup reads the newest stored version. Asked as of a commit, it reads the
version stored for exactly that commit; a commit with no stored version is a typed not-found that
names the nearest stored version and never the head's answer. The answer reads no file: the
versions are the whole input.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFault,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
    ModelCodeGraphVersion,
    ModelLookupMatch,
    ModelNearestVersion,
    ModelShapeEntry,
)
from omnimarket.nodes.node_code_graph_effect.protocols import (
    ProtocolCodeGraphVersionStore,
    ProtocolCommitClock,
)

#: An exemplar answer names at most this many nodes, best first.
EXEMPLAR_LIMIT = 5


class CodeGraphLookup:
    """Answer a lookup command from the version store."""

    def __init__(
        self, store: ProtocolCodeGraphVersionStore, clock: ProtocolCommitClock
    ) -> None:
        self._store = store
        self._clock = clock

    def handle(
        self, command: ModelCodeGraphLookupCommand
    ) -> ModelCodeGraphLookupResult:
        as_of = (
            None if command.fault is EnumLookupFault.ANSWER_FROM_HEAD else command.as_of
        )
        version = (
            self._store.newest(command.repo)
            if as_of is None
            else self._store.get(command.repo, as_of)
        )
        if version is None:
            return self._no_version(command, as_of)
        matches = _answer(version, command)
        return ModelCodeGraphLookupResult(
            correlation_id=command.correlation_id,
            question=command.question,
            repo=command.repo,
            status="found" if matches else "not-found",
            answered_from=version.commit,
            matches=matches,
            reason=None if matches else "the version holds no answer to the question",
        )

    def _no_version(
        self, command: ModelCodeGraphLookupCommand, as_of: str | None
    ) -> ModelCodeGraphLookupResult:
        if as_of is None:
            return ModelCodeGraphLookupResult(
                correlation_id=command.correlation_id,
                question=command.question,
                repo=command.repo,
                status="not-found",
                reason=f"no version is stored for {command.repo}",
            )
        return ModelCodeGraphLookupResult(
            correlation_id=command.correlation_id,
            question=command.question,
            repo=command.repo,
            status="not-found",
            nearest=self._nearest(command.repo, as_of),
            reason=f"no version is stored for {command.repo} at {as_of}",
        )

    def _nearest(self, repo: str, as_of: str) -> ModelNearestVersion | None:
        versions = self._store.versions(repo)
        if not versions:
            return None
        asked = self._clock.committed_at(repo, as_of)
        dated = [version for version in versions if version.committed_at is not None]
        if asked is not None and dated:
            best = min(
                dated,
                key=lambda version: (_gap(version.committed_at, asked), version.commit),
            )
            return ModelNearestVersion(
                commit=best.commit,
                committed_at=best.committed_at,
                ordering="by-commit-time",
            )
        newest = versions[-1]
        return ModelNearestVersion(
            commit=newest.commit,
            committed_at=newest.committed_at,
            ordering="unresolved",
        )


def _gap(stored: datetime | None, asked: datetime) -> tuple[float, float]:
    """Distance to the asked time; an earlier version wins a tie."""
    if stored is None:
        raise ValueError("an undated version has no distance")
    seconds = (stored - asked).total_seconds()
    return abs(seconds), seconds


def _answer(
    version: ModelCodeGraphVersion, command: ModelCodeGraphLookupCommand
) -> tuple[ModelLookupMatch, ...]:
    question = command.question
    if question is EnumCodeGraphQuestion.DEFINITION:
        return _definition(version, command.symbol or "")
    if question is EnumCodeGraphQuestion.IMPORTERS:
        return _importers(version, command.path or "")
    if question is EnumCodeGraphQuestion.REACH:
        return _reach(version, command.paths)
    if question is EnumCodeGraphQuestion.EXEMPLAR:
        return _exemplar(version, command.archetype or "", command.hint)
    return _same_shape(
        version,
        command.symbol or "",
        by_name=command.fault is EnumLookupFault.MATCH_SHAPES_BY_NAME,
    )


def _definition(
    version: ModelCodeGraphVersion, symbol: str
) -> tuple[ModelLookupMatch, ...]:
    return tuple(
        ModelLookupMatch(
            path=definition.path,
            name=definition.name,
            line=definition.line,
            detail=definition.kind,
        )
        for definition in sorted(
            (d for d in version.definitions if d.name == symbol),
            key=lambda d: (d.path, d.line),
        )
    )


def _importers(
    version: ModelCodeGraphVersion, path: str
) -> tuple[ModelLookupMatch, ...]:
    return tuple(
        ModelLookupMatch(path=importer)
        for importer in sorted(
            {r.importer_path for r in version.imports if r.imported_path == path}
        )
    )


def _reach(
    version: ModelCodeGraphVersion, paths: tuple[str, ...]
) -> tuple[ModelLookupMatch, ...]:
    importers: dict[str, set[str]] = {}
    for relation in version.imports:
        importers.setdefault(relation.imported_path, set()).add(relation.importer_path)
    depth = dict.fromkeys(paths, 0)
    queue = deque(paths)
    while queue:
        current = queue.popleft()
        for importer in sorted(importers.get(current, ())):
            if importer not in depth:
                depth[importer] = depth[current] + 1
                queue.append(importer)
    return tuple(
        ModelLookupMatch(path=path, detail=f"depth {hops}")
        for path, hops in sorted(depth.items(), key=lambda item: (item[1], item[0]))
        if path not in paths
    )


def _exemplar(
    version: ModelCodeGraphVersion, archetype: str, hint: str | None
) -> tuple[ModelLookupMatch, ...]:
    words = [word for word in (hint or "").lower().replace("_", " ").split() if word]

    def rank(name: str) -> int:
        return sum(1 for word in words if word in name.lower())

    nodes = sorted(
        (node for node in version.exemplars if node.archetype == archetype),
        key=lambda node: (-rank(node.name), node.path),
    )
    return tuple(
        ModelLookupMatch(path=node.path, name=node.name, detail=node.archetype)
        for node in nodes[:EXEMPLAR_LIMIT]
    )


def _same_shape(
    version: ModelCodeGraphVersion, symbol: str, *, by_name: bool
) -> tuple[ModelLookupMatch, ...]:
    own = [shape for shape in version.shapes if shape.name == symbol]
    own_keys = {(shape.path, shape.name) for shape in own}

    # The break compares class names where the lookup compares field shapes.
    def key(shape: ModelShapeEntry) -> str:
        return shape.name if by_name else shape.signature

    wanted = {key(shape) for shape in own}
    found = [
        shape
        for shape in version.shapes
        if key(shape) in wanted and (shape.path, shape.name) not in own_keys
    ]
    return tuple(
        ModelLookupMatch(
            path=shape.path,
            name=shape.name,
            line=shape.line,
            detail=f"shape {shape.signature[:12]}",
        )
        for shape in sorted(found, key=lambda s: (s.path, s.line))
    )
