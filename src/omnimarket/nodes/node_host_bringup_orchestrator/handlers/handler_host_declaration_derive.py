# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostDeclarationDerive: one host declaration, every other copy derived (OMN-21899).

Pure: no clock, file, network or shell. The owner copy's rows are the declaration.
Each derived surface carries ``name`` and the fields its spec lists, valued from the
declaration, in declaration order. Each copy as read is compared with what it must
be, in both directions, so a hand-added row, column or value in any copy is named
by surface, host and field. This is the check of the ``declared`` target
(``declaration_drift``) and the body of its ``data_pr`` action.

``flip_in_service`` is the ``in-service`` action: it removes ``available`` and
``unavailable_reason`` from the named hosts' declaration rows (omitted means
available), and every copy follows from the flipped declaration. A flip of a host
the declaration does not hold is refused.
"""

from __future__ import annotations

import json

from pydantic import JsonValue

from omnimarket.nodes.node_host_bringup_orchestrator.models.model_host_declaration import (
    EnumCopyDifferenceKind,
    ModelHostCopyDifference,
    ModelHostDeclarationDerived,
    ModelHostDeclarationDeriveRequest,
)

_FLIP_REMOVES = ("available", "unavailable_reason")


def _canon(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _host(row: dict[str, JsonValue]) -> str:
    name = row.get("name")
    return name if isinstance(name, str) else ""


class HandlerHostDeclarationDerive:
    """Derive every copy of the host table from the declaration and name each difference."""

    def handle(
        self, request: ModelHostDeclarationDeriveRequest
    ) -> ModelHostDeclarationDerived:
        refusals: list[str] = []
        rows = [dict(r) for r in request.declaration]
        names = [_host(r) for r in rows]
        if "" in names:
            refusals.append("a declaration row has no name")
        for dup in sorted({n for n in names if names.count(n) > 1}):
            refusals.append(f"host {dup} is declared more than once")
        for host in request.flip_in_service:
            if host not in names:
                refusals.append(
                    f"flip of {host} refused: the declaration holds no such host"
                )
        unknown = sorted(set(request.copies) - {s.surface for s in request.surfaces})
        for surface in unknown:
            refusals.append(
                f"a copy names surface {surface}, which no surface spec declares"
            )
        if refusals:
            return ModelHostDeclarationDerived(refusals=tuple(refusals))

        for row in rows:
            if _host(row) in request.flip_in_service:
                for key in _FLIP_REMOVES:
                    row.pop(key, None)
        declaration = tuple(rows)

        derived: dict[str, tuple[dict[str, JsonValue], ...]] = {}
        differences: list[ModelHostCopyDifference] = []
        for spec in request.surfaces:
            carried = ("name", *(f for f in spec.fields if f != "name"))
            want_rows = tuple(
                {k: row[k] for k in carried if k in row} for row in declaration
            )
            derived[spec.surface] = want_rows
            if spec.surface in request.copies:
                differences.extend(
                    self._compare(spec.surface, want_rows, request.copies[spec.surface])
                )

        return ModelHostDeclarationDerived(
            declaration=declaration,
            derived=derived,
            differences=tuple(differences),
            in_sync=not differences,
        )

    @staticmethod
    def _compare(
        surface: str,
        want_rows: tuple[dict[str, JsonValue], ...],
        have_rows: tuple[dict[str, JsonValue], ...],
    ) -> list[ModelHostCopyDifference]:
        want = {_host(r): r for r in want_rows}
        have = {_host(r): r for r in have_rows}
        out: list[ModelHostCopyDifference] = []
        # A copy holding one host twice is a difference of its own: the dict above keeps only the last row.
        have_names = [_host(r) for r in have_rows]
        for host in sorted({n for n in have_names if have_names.count(n) > 1}):
            out.append(
                ModelHostCopyDifference(
                    surface=surface,
                    host=host,
                    kind=EnumCopyDifferenceKind.DUPLICATE_HOST,
                )
            )
        for host in sorted(set(want) | set(have)):
            if host not in have:
                out.append(
                    ModelHostCopyDifference(
                        surface=surface,
                        host=host,
                        kind=EnumCopyDifferenceKind.MISSING_HOST,
                    )
                )
                continue
            if host not in want:
                out.append(
                    ModelHostCopyDifference(
                        surface=surface,
                        host=host,
                        kind=EnumCopyDifferenceKind.SURPLUS_HOST,
                    )
                )
                continue
            w, h = want[host], have[host]
            for field in sorted(set(w) | set(h)):
                if field not in h:
                    kind = EnumCopyDifferenceKind.MISSING_FIELD
                elif field not in w:
                    kind = EnumCopyDifferenceKind.SURPLUS_FIELD
                elif _canon(w[field]) != _canon(h[field]):
                    kind = EnumCopyDifferenceKind.VALUE_DIFFERS
                else:
                    continue
                out.append(
                    ModelHostCopyDifference(
                        surface=surface,
                        host=host,
                        field=field,
                        kind=kind,
                        have=_canon(h[field]) if field in h else None,
                        want=_canon(w[field]) if field in w else None,
                    )
                )
        return out


__all__ = ["HandlerHostDeclarationDerive"]
