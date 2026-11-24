# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lookup: the questions answered from one stored version of a repository's graph (OMN-23648).

Asked at the head, a lookup reads the newest stored version. Asked as of a commit, it reads the
version stored for exactly that commit; a commit with no stored version is a typed not-found that
names the nearest stored version and never the head's answer. The answer reads no file: the
versions are the whole input.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from omnimarket.models.model_code_graph import (
    EnumWrittenImportKind,
    ModelFileReferences,
    ModelWrittenImport,
)
from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFault,
    EnumReferenceForm,
    EnumRemovalKind,
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
#: The reason prefix of a locate asked of a version stored before node files were indexed.
LOCATE_NOT_INDEXED = "question-not-indexed"


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
        if command.question is EnumCodeGraphQuestion.LOCATE and not version.node_files:
            return ModelCodeGraphLookupResult(
                correlation_id=command.correlation_id,
                question=command.question,
                repo=command.repo,
                status="not-found",
                answered_from=version.commit,
                reason=f"{LOCATE_NOT_INDEXED}: the stored version of {command.repo} holds no node files",
            )
        if command.question is EnumCodeGraphQuestion.HANDLERS:
            return _handlers(version, command)
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


def _handlers(
    version: ModelCodeGraphVersion, command: ModelCodeGraphLookupCommand
) -> ModelCodeGraphLookupResult:
    """Every operation row of the version; one match per row keeps the match listing readable."""
    rows = version.handler_rows
    return ModelCodeGraphLookupResult(
        correlation_id=command.correlation_id,
        question=command.question,
        repo=command.repo,
        status="found" if rows else "not-found",
        answered_from=version.commit,
        handler_rows=rows,
        matches=tuple(
            ModelLookupMatch(
                path=row.contract,
                name=row.operation,
                detail=f"{row.resolution.value} {row.route.value}",
            )
            for row in rows
        ),
        reason=None if rows else "the version holds no handler routing",
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
    if question is EnumCodeGraphQuestion.USERS:
        return _users(version, command)
    if question is EnumCodeGraphQuestion.LOCATE:
        return _locate(version, command.target or "")
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


def _locate(
    version: ModelCodeGraphVersion, target: str
) -> tuple[ModelLookupMatch, ...]:
    """Every file of each node the target is: its name, or any one of its stored paths."""
    wanted = target.rstrip("/")
    nodes = {f.node for f in version.node_files if wanted in (f.node, f.path)}
    return tuple(
        ModelLookupMatch(path=f.path, name=f.node, detail=f.kind.value)
        for f in version.node_files
        if f.node in nodes
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


def _within(dotted: str, identity: str) -> bool:
    return dotted == identity or dotted.startswith(identity + ".")


def _import_form(
    references: ModelFileReferences, written: ModelWrittenImport
) -> EnumReferenceForm:
    """A package ``__init__``, ``import x as x`` and a name in the file's ``__all__`` re-export."""
    if (
        references.path.rsplit("/", 1)[-1] == "__init__.py"
        or (written.alias is not None and written.alias == written.name)
        or (written.name is not None and written.name in references.all_names)
    ):
        return EnumReferenceForm.RE_EXPORT
    if written.kind is EnumWrittenImportKind.IMPORT_MODULE:
        return EnumReferenceForm.IMPORT_MODULE_LITERAL
    return EnumReferenceForm.DIRECT_IMPORT


def _spelled(written: ModelWrittenImport) -> str:
    if written.kind is EnumWrittenImportKind.FROM:
        return f"from {written.module} import {written.name}"
    if written.kind is EnumWrittenImportKind.STAR:
        return f"from {written.module} import *"
    return f"import {written.module}"


def _users(
    version: ModelCodeGraphVersion, command: ModelCodeGraphLookupCommand
) -> tuple[ModelLookupMatch, ...]:
    """Each written import (or use of what one bound) that reaches the removed identity.

    Matched against the dotted identity as written, never resolved against a head: an import of a
    name that no longer exists is still found.
    """
    identity = command.identity or ""
    module_removal = command.removal_kind is EnumRemovalKind.PYTHON_MODULE
    parent, _, leaf = identity.rpartition(".")
    rebound = frozenset(command.rebound_submodules)
    found: dict[tuple[str, int, str], ModelLookupMatch] = {}

    def keep(
        path: str, line: int, form: EnumReferenceForm, text: str, detail: str = ""
    ) -> None:
        found.setdefault(
            (path, line, form.value),
            ModelLookupMatch(
                path=path,
                name=text,
                line=line,
                detail=f"{form.value}{detail}",
                reference_form=form,
            ),
        )

    for references in version.references:
        uses = {use.name: use.line for use in references.free_uses}
        for written in references.imports:
            qualified = (
                f"{written.module}.{written.name}"
                if written.kind is EnumWrittenImportKind.FROM
                else written.module
            )
            if written.kind is EnumWrittenImportKind.STAR:
                if module_removal:
                    hit = written.module == identity
                    detail = ""
                else:
                    exported = (
                        command.star_export_list is not None
                        and leaf in command.star_export_list
                    )
                    used = leaf in uses
                    hit = (
                        written.module == parent
                        and exported
                        and (command.head_all_lists_name or used)
                    )
                    detail = (
                        f"; head __all__ lists {leaf}"
                        if command.head_all_lists_name
                        else f"; bare use at line {uses.get(leaf)}"
                    )
                if hit:
                    keep(
                        references.path,
                        written.line,
                        EnumReferenceForm.STAR_IMPORT,
                        _spelled(written),
                        detail,
                    )
            elif module_removal:
                if _within(written.module, identity) or (
                    written.kind is EnumWrittenImportKind.FROM
                    and qualified == identity
                    and identity not in rebound
                ):
                    keep(
                        references.path,
                        written.line,
                        _import_form(references, written),
                        _spelled(written),
                    )
            elif written.kind is EnumWrittenImportKind.FROM and qualified == identity:
                keep(
                    references.path,
                    written.line,
                    _import_form(references, written),
                    _spelled(written),
                )
        if module_removal:
            continue
        for use in references.attribute_uses:
            deeper = use.anchor != identity and identity.startswith(use.anchor + ".")
            if (
                deeper
                and _within(use.dotted, identity)
                and not (use.via == "from-name" and use.anchor in rebound)
            ):
                keep(
                    references.path,
                    use.line,
                    EnumReferenceForm.ATTRIBUTE_USE,
                    use.dotted,
                )
    return tuple(
        sorted(found.values(), key=lambda m: (m.path, m.line or 0, m.detail or ""))
    )
