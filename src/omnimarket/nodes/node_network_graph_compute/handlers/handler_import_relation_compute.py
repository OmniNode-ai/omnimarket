# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerImportRelationCompute: import edges in, directed relations, cycles and the join map out.

Pure: no file, clock, network or environment. Every direction of every label pair is its own
relation, so two labels that import each other read one cycle. The old graph kept only the
stronger direction of a pair, which left the cycle report empty; the request's
``keep_stronger_direction_only`` switch reproduces that on purpose, as the injected break.
"""

from __future__ import annotations

from collections import defaultdict

from omnimarket.models.import_relations import (
    EnumRelationFindingKind,
    ModelImportEdge,
    ModelImportRelationOverlay,
    ModelImportRelationReport,
    ModelImportRelationRequest,
    ModelRelation,
    ModelRelationCycle,
    ModelRelationFinding,
    ModelRelationJoinEntry,
    ModelRelationKey,
)

Pair = tuple[str, str]
Entity = tuple[str, str]


def label_of(overlay: ModelImportRelationOverlay, repo: str, path: str) -> str:
    """The label of a file: the first rule that matches it, else its repository."""
    for rule in overlay.label_rules:
        if rule.repo == repo and path.startswith(rule.path_prefix):
            return rule.label
    return repo


def _occurrences(
    edges: tuple[ModelImportEdge, ...], overlay: ModelImportRelationOverlay
) -> dict[Pair, set[tuple[Entity, Entity]]]:
    found: dict[Pair, set[tuple[Entity, Entity]]] = defaultdict(set)
    for edge in edges:
        importer = (edge.importer_repo, edge.importer_path)
        imported = (edge.imported_repo, edge.imported_path)
        source = label_of(overlay, *importer)
        target = label_of(overlay, *imported)
        if source != target:
            found[(source, target)].add((importer, imported))
    return found


def _keep_stronger(found: dict[Pair, set[tuple[Entity, Entity]]]) -> None:
    """Drop the weaker direction of each pair in place (ties keep the lower source name)."""
    for source, target in sorted(found):
        reverse = (target, source)
        if (source, target) not in found or reverse not in found:
            continue
        mine, theirs = len(found[(source, target)]), len(found[reverse])
        if mine < theirs or (mine == theirs and source > target):
            del found[(source, target)]


def _relation(pair: Pair, pairs: set[tuple[Entity, Entity]]) -> ModelRelation:
    return ModelRelation(
        key=ModelRelationKey(source=pair[0], target=pair[1]),
        occurrences=len(pairs),
        entities=len({entity for edge in pairs for entity in edge}),
        files=len({importer for importer, _ in pairs}),
    )


def _cycles(
    found: dict[Pair, set[tuple[Entity, Entity]]],
) -> tuple[ModelRelationCycle, ...]:
    """The strongly connected sets of two or more labels, found by reachability."""
    successors: dict[str, set[str]] = defaultdict(set)
    for source, target in found:
        successors[source].add(target)

    def reach(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            for nxt in successors[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    reached = {label: reach(label) for label in sorted(successors)}
    groups: set[tuple[str, ...]] = set()
    for label, targets in reached.items():
        members = {label} | {t for t in targets if label in reached.get(t, ())}
        if len(members) > 1:
            groups.add(tuple(sorted(members)))
    cycles = []
    for labels in sorted(groups):
        inside = sorted(
            pair for pair in found if pair[0] in labels and pair[1] in labels
        )
        cycles.append(
            ModelRelationCycle(
                labels=labels,
                relations=tuple(
                    ModelRelationKey(source=s, target=t) for s, t in inside
                ),
                occurrences=sum(len(found[pair]) for pair in inside),
            )
        )
    return tuple(cycles)


def _joins_of(
    overlay: ModelImportRelationOverlay, piece: int | None
) -> tuple[str, ...]:
    if piece is None:
        return ()
    return tuple(sorted(j.join_id for j in overlay.joins if piece in j.pieces))


def _map_relation(
    key: ModelRelationKey,
    occurrences: int,
    overlay: ModelImportRelationOverlay,
) -> tuple[ModelRelationJoinEntry, tuple[ModelRelationFinding, ...]]:
    owners = {owner.label: owner.piece for owner in overlay.label_owners}
    source_piece = owners.get(key.source)
    target_piece = owners.get(key.target)
    source_joins = _joins_of(overlay, source_piece)
    target_joins = _joins_of(overlay, target_piece)
    findings: list[ModelRelationFinding] = []
    for label, piece, joins in (
        (key.source, source_piece, source_joins),
        (key.target, target_piece, target_joins),
    ):
        if piece is None:
            findings.append(
                ModelRelationFinding(
                    relation=key,
                    kind=EnumRelationFindingKind.UNMAPPED_ENDPOINT,
                    detail=f"no piece owns {label}",
                )
            )
        elif not joins:
            findings.append(
                ModelRelationFinding(
                    relation=key,
                    kind=EnumRelationFindingKind.PIECE_WITHOUT_JOIN,
                    detail=f"piece {piece} owning {label} carries no join",
                )
            )
    if occurrences == 0:
        findings.append(
            ModelRelationFinding(
                relation=key,
                kind=EnumRelationFindingKind.NOT_IN_GRAPH,
                detail="the graph holds no occurrence of this relation",
            )
        )
    entry = ModelRelationJoinEntry(
        relation=key,
        occurrences=occurrences,
        source_piece=source_piece,
        target_piece=target_piece,
        source_joins=source_joins,
        target_joins=target_joins,
        mapped=bool(source_joins) and bool(target_joins),
    )
    return entry, tuple(findings)


class HandlerImportRelationCompute:
    """Directed relations, cycles and the join map from import edges."""

    def handle(self, request: ModelImportRelationRequest) -> ModelImportRelationReport:
        found = _occurrences(request.edges, request.overlay)
        if request.keep_stronger_direction_only:
            _keep_stronger(found)
        entries: list[ModelRelationJoinEntry] = []
        findings: list[ModelRelationFinding] = []
        for key in sorted(
            request.overlay.tracked_relations, key=lambda k: (k.source, k.target)
        ):
            entry, problems = _map_relation(
                key, len(found.get((key.source, key.target), ())), request.overlay
            )
            entries.append(entry)
            findings.extend(problems)
        return ModelImportRelationReport(
            generated_at=request.reference_time,
            relations=tuple(_relation(pair, found[pair]) for pair in sorted(found)),
            cycles=_cycles(found),
            relation_map=tuple(entries),
            findings=tuple(findings),
        )
