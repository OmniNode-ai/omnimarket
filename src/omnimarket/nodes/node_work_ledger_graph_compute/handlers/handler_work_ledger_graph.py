# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive the work ledger graph from the rows' own key=value cells.

Pure and deterministic: no model call, no I/O. Each row is split on its
`` | `` cell separator; a cell shaped ``key=value`` is a typed cell, and the
edge rules below map typed cells to entity nodes. Rows are ordered by
(row_ts, row_id) and edges by (row_id, kind, target), and the content hash is
taken over the canonical JSON, so the same rows give byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
import re

from omnimarket.models.work_ledger_graph.model_work_ledger_graph import (
    EnumLedgerEdgeKind,
    ModelLedgerGraphCell,
    ModelLedgerGraphEdge,
    ModelLedgerGraphRow,
    ModelLedgerGraphRowNode,
    ModelWorkLedgerGraph,
    ModelWorkLedgerGraphComputeRequest,
)

__all__ = ["HandlerWorkLedgerGraph", "derive_graph", "normalize_pr", "parse_cells"]

_CELL = re.compile(r"^([a-z][A-Za-z0-9_-]*)=(.*)$", re.DOTALL)
_PR = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?([A-Za-z0-9_.-]+)#(\d+)$")

# Cells whose value is an entity reference, and the edge each one makes.
_SINGLE: dict[str, tuple[EnumLedgerEdgeKind, str]] = {
    "lane": (EnumLedgerEdgeKind.IN_LANE, "lane"),
    "ticket": (EnumLedgerEdgeKind.NAMES_TICKET, "ticket"),
    "parent_ticket": (EnumLedgerEdgeKind.PARENT_TICKET, "ticket"),
    "mechanism-ticket": (EnumLedgerEdgeKind.MECHANISM_TICKET, "ticket"),
    "mechanism_ticket": (EnumLedgerEdgeKind.MECHANISM_TICKET, "ticket"),
    "parent": (EnumLedgerEdgeKind.PARENT_LANE, "lane"),
    "head": (EnumLedgerEdgeKind.NAMES_HEAD, "head"),
    "installed": (EnumLedgerEdgeKind.INSTALLED, "installed"),
    "from": (EnumLedgerEdgeKind.MSG_FROM, "lane"),
    "to": (EnumLedgerEdgeKind.MSG_TO, "lane"),
    "handed_to": (EnumLedgerEdgeKind.HANDED_TO, "lane"),
    "cause": (EnumLedgerEdgeKind.NAMES_CAUSE, "cause"),
}
_PR_KEYS = ("pr", "prs", "handed-off")
# A check= cell may list several checks separated by commas.
_CHECK_KEYS = ("check", "checks")


def normalize_pr(value: str) -> str | None:
    """``OmniNode-ai/omnimarket#12`` and ``omnimarket#12`` both name ``omnimarket#12``."""
    match = _PR.match(value.strip())
    if match is None:
        return None
    return f"{match.group(1)}#{match.group(2)}"


def parse_cells(raw_row: str) -> tuple[ModelLedgerGraphCell, ...]:
    """The typed cells of a row's first line, in their order on the row."""
    first_line = raw_row.split("\n", 1)[0]
    parts = [part.strip() for part in first_line.split(" | ")]
    cells: list[ModelLedgerGraphCell] = []
    for part in parts[2:]:
        match = _CELL.match(part)
        if match is None:
            continue
        # A cell written with spaces where the separator belongs
        # ("verdict=X head=Y pr=Z") is split only when every token is key=value.
        tokens = part.split()
        if len(tokens) > 1 and all(_CELL.match(token) for token in tokens):
            for token in tokens:
                key, _, value = token.partition("=")
                cells.append(ModelLedgerGraphCell(key=key, value=value))
            continue
        cells.append(
            ModelLedgerGraphCell(key=match.group(1), value=match.group(2).strip())
        )
    return tuple(cells)


def _row_edges(
    row_id: str,
    row_type: str,
    row_ts: str,
    lane: str | None,
    cells: tuple[ModelLedgerGraphCell, ...],
) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for cell in cells:
        if not cell.value:
            continue
        if cell.key in _SINGLE:
            kind, prefix = _SINGLE[cell.key]
            edges.add((kind.value, f"{prefix}:{cell.value}"))
        elif cell.key in _PR_KEYS:
            for item in cell.value.split(","):
                pr = normalize_pr(item)
                if pr is not None:
                    edges.add((EnumLedgerEdgeKind.NAMES_PR.value, f"pr:{pr}"))
        elif cell.key in _CHECK_KEYS:
            for item in cell.value.split(","):
                if item.strip():
                    edges.add(
                        (EnumLedgerEdgeKind.NAMES_CHECK.value, f"check:{item.strip()}")
                    )
        elif cell.key == "closes-CLAIM" and lane is not None:
            edges.add(
                (EnumLedgerEdgeKind.CLOSES_CLAIM.value, f"claim:{lane}@{cell.value}")
            )
    if row_type == "CLAIM" and lane is not None:
        edges.add((EnumLedgerEdgeKind.OPENS_CLAIM.value, f"claim:{lane}@{row_ts}"))
    return edges


def derive_graph(rows: tuple[ModelLedgerGraphRow, ...]) -> ModelWorkLedgerGraph:
    ordered = sorted(rows, key=lambda r: (r.row_ts, r.row_id))
    row_nodes: list[ModelLedgerGraphRowNode] = []
    edges: list[ModelLedgerGraphEdge] = []
    seen: set[str] = set()
    for row in ordered:
        if row.row_id in seen:
            continue
        seen.add(row.row_id)
        cells = parse_cells(row.raw_row)
        lane = next((c.value for c in cells if c.key == "lane" and c.value), None)
        row_type = row.row_type.strip().upper()
        row_nodes.append(
            ModelLedgerGraphRowNode(
                row_id=row.row_id,
                row_ts=row.row_ts,
                row_type=row_type,
                lane=lane,
                cells=cells,
            )
        )
        for kind, target in sorted(
            _row_edges(row.row_id, row_type, row.row_ts, lane, cells)
        ):
            edges.append(
                ModelLedgerGraphEdge(
                    row_id=row.row_id, kind=EnumLedgerEdgeKind(kind), target=target
                )
            )
    edges.sort(key=lambda e: (e.row_id, e.kind.value, e.target))
    entities = {edge.target for edge in edges}
    unhashed = ModelWorkLedgerGraph(
        row_count=len(row_nodes),
        entity_count=len(entities),
        rows=tuple(row_nodes),
        edges=tuple(edges),
    )
    canonical = json.dumps(
        unhashed.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return unhashed.model_copy(update={"content_sha256": digest})


class HandlerWorkLedgerGraph:
    """ONEX compute handler: rows in, graph out."""

    def handle(
        self, request: ModelWorkLedgerGraphComputeRequest
    ) -> ModelWorkLedgerGraph:
        return derive_graph(request.rows)
