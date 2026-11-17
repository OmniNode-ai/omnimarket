# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive the work ledger graph from the rows' own ``key=value`` cells.

Pure and deterministic: no I/O, no clock, no model call. The same rows, in any
order, give the same nodes, the same edges and the same digest. Shared by
``node_work_ledger_graph_compute`` (which returns the graph) and
``node_work_ledger_graph_query_effect`` (which answers the five typed questions
over it), so neither node imports the other's handler.

Edges and the cells they come from:

- ``row -WRITTEN_BY-> lane``      ``lane=`` (or the row_lane column)
- ``row -CITES-> ticket``         ``ticket=`` and the tickets column
- ``lane -CHILD_OF-> lane``       ``parent=``
- ``ticket -CHILD_OF-> ticket``   ``parent_ticket=``
- ``row -NAMES_PR-> pr``          ``pr=``, ``fix-pr=``
- ``row -HANDS_OFF-> pr``         ``handed-off=``
- ``pr -AT_HEAD-> head``          ``pr=`` and ``head=`` on one row naming one PR
- ``row -NAMES_HEAD-> head``      ``head=``
- ``head -INSTALLED_AS-> install`` ``installed=<host>:<rev>`` beside ``head=``
- ``row -MSG_FROM/MSG_TO-> lane`` ``from=``, ``to=``
- ``row -CLOSES-> row``           a TERMINAL's ``closes-CLAIM=<ts>`` to its lane's CLAIM at that ts
- ``row -MECHANISM-> ticket``     a RULING's ``mechanism-ticket=``
- ``row -NAMES_CHECK-> check`` and ``check -HOLDS_PR-> pr``  ``check=`` beside a PR cell
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC

from omnimarket.models.work_ledger_graph import (
    ModelWorkLedgerGraph,
    ModelWorkLedgerGraphEdge,
    ModelWorkLedgerGraphNode,
    ModelWorkLedgerGraphRow,
)

_CELL = re.compile(r"^([A-Za-z][\w-]*)=(.*)$", re.DOTALL)
_PR = re.compile(r"(?:[\w.-]+/)?([A-Za-z][\w.-]*)#(\d+)")
_TICKET = re.compile(r"\bOMN-\d+\b")
_HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")
_NONE = {"", "none", "na", "n/a", "-", "unknown", "none:none"}

# Row attributes kept on a row node: the cells the five queries read.
_ROW_ATTR_CELLS = (
    "verdict",
    "outcome",
    "kind",
    "merged",
    "action",
    "handed_to",
    "to",
    "from",
    "closes-CLAIM",
    "result",
)


def parse_cells(raw_row: str) -> dict[str, list[str]]:
    """Return every ``key=value`` cell of a row, in row order, keyed by name.

    The first two fields (timestamp and TYPE) and free-text cells are skipped.
    """
    first_line = raw_row.split("\n", 1)[0]
    cells: dict[str, list[str]] = {}
    for part in first_line.split(" | ")[2:]:
        match = _CELL.match(part.strip())
        if match is None:
            continue
        cells.setdefault(match.group(1), []).append(match.group(2).strip())
    return cells


def is_real(value: str) -> bool:
    return value.strip().lower() not in _NONE and not value.strip().lower().startswith(
        "none"
    )


def normalize_prs(values: Iterable[str]) -> list[str]:
    """``OmniNode-ai/omnimarket#6132`` and ``omnimarket#6132`` both become ``omnimarket#6132``."""
    out: list[str] = []
    for value in values:
        for match in _PR.finditer(value):
            ref = f"{match.group(1)}#{match.group(2)}"
            if ref not in out:
                out.append(ref)
    return out


def normalize_head(value: str) -> str | None:
    token = value.strip().split()[0] if value.strip() else ""
    if not _HEX.match(token):
        return None
    return token[:12].lower()


def normalize_install(value: str) -> str | None:
    """``h201:baa2bc9cb291...`` (optionally followed by prose) becomes ``h201:baa2bc9cb291``."""
    if not is_real(value):
        return None
    token = value.strip().split()[0]
    host, sep, rev = token.partition(":")
    if not sep or not host or not is_real(rev):
        return None
    head = normalize_head(rev)
    return f"{host}:{head}" if head else f"{host}:{rev}"


def tickets_of(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for ticket in _TICKET.findall(value):
            if ticket not in out:
                out.append(ticket)
    return out


def _ts(row: ModelWorkLedgerGraphRow) -> str:
    return row.row_ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_graph(rows: Iterable[ModelWorkLedgerGraphRow]) -> ModelWorkLedgerGraph:
    """Derive the graph. Order of ``rows`` does not change the output."""
    unique: dict[str, ModelWorkLedgerGraphRow] = {}
    for row in rows:
        unique.setdefault(row.row_id, row)
    ordered = sorted(unique.values(), key=lambda r: (r.row_ts, r.row_id))

    nodes: dict[str, tuple[str, dict[str, str]]] = {}
    edges: set[tuple[str, str, str, str]] = set()

    def node(node_id: str, node_kind: str, attrs: dict[str, str] | None = None) -> str:
        if node_id not in nodes:
            nodes[node_id] = (node_kind, {})
        if attrs:
            nodes[node_id][1].update({k: v for k, v in attrs.items() if v})
        return node_id

    claims: dict[tuple[str, str], list[str]] = {}
    parsed: list[tuple[ModelWorkLedgerGraphRow, dict[str, list[str]], str | None]] = []
    for row in ordered:
        cells = parse_cells(row.raw_row)
        lane = (cells.get("lane") or [row.row_lane or ""])[0].strip() or None
        parsed.append((row, cells, lane))
        if row.row_type == "CLAIM" and lane:
            claims.setdefault((lane, _ts(row)), []).append(row.row_id)

    for row, cells, lane in parsed:
        row_node = f"row:{row.row_id}"
        attrs = {"type": row.row_type, "ts": _ts(row)}
        if lane:
            attrs["lane"] = lane
        for key in _ROW_ATTR_CELLS:
            if key in cells:
                attrs[key] = cells[key][0]
        install = next(
            (
                i
                for i in (normalize_install(v) for v in cells.get("installed", []))
                if i
            ),
            None,
        )
        observed = any(is_real(v) for v in cells.get("observed", []))
        if install:
            attrs["installed"] = install
        if observed:
            attrs["observed"] = "yes"
        node(row_node, "row", attrs)

        if lane:
            edges.add(
                (row_node, "WRITTEN_BY", node(f"lane:{lane}", "lane"), row.row_id)
            )
        for ticket in tickets_of([*cells.get("ticket", []), *row.tickets]):
            edges.add(
                (row_node, "CITES", node(f"ticket:{ticket}", "ticket"), row.row_id)
            )
        for parent in cells.get("parent", []):
            if lane and is_real(parent) and not _TICKET.fullmatch(parent):
                edges.add(
                    (
                        f"lane:{lane}",
                        "CHILD_OF",
                        node(f"lane:{parent}", "lane"),
                        row.row_id,
                    )
                )
        own_tickets = tickets_of(cells.get("ticket", []))
        for parent_ticket in tickets_of(cells.get("parent_ticket", [])):
            for child in own_tickets:
                edges.add(
                    (
                        node(f"ticket:{child}", "ticket"),
                        "CHILD_OF",
                        node(f"ticket:{parent_ticket}", "ticket"),
                        row.row_id,
                    )
                )
        named_prs = normalize_prs([*cells.get("pr", []), *cells.get("fix-pr", [])])
        for pr in named_prs:
            edges.add((row_node, "NAMES_PR", node(f"pr:{pr}", "pr"), row.row_id))
        for pr in normalize_prs(cells.get("handed-off", [])):
            edges.add((row_node, "HANDS_OFF", node(f"pr:{pr}", "pr"), row.row_id))
        heads = [h for h in (normalize_head(v) for v in cells.get("head", [])) if h]
        for head in heads:
            head_node = node(f"head:{head}", "head")
            edges.add((row_node, "NAMES_HEAD", head_node, row.row_id))
            if len(named_prs) == 1:
                edges.add((f"pr:{named_prs[0]}", "AT_HEAD", head_node, row.row_id))
            if install:
                edges.add(
                    (
                        head_node,
                        "INSTALLED_AS",
                        node(f"install:{install}", "install"),
                        row.row_id,
                    )
                )
        for key, rel in (("from", "MSG_FROM"), ("to", "MSG_TO")):
            if row.row_type in {"MSG", "ACK", "HOLD", "RELEASE"}:
                for value in cells.get(key, []):
                    if is_real(value):
                        edges.add(
                            (row_node, rel, node(f"lane:{value}", "lane"), row.row_id)
                        )
        if row.row_type == "TERMINAL" and lane:
            for closes in cells.get("closes-CLAIM", []):
                for claim_id in claims.get((lane, closes.strip()), []):
                    edges.add((row_node, "CLOSES", f"row:{claim_id}", row.row_id))
        if row.row_type == "RULING":
            for ticket in tickets_of(cells.get("mechanism-ticket", [])):
                edges.add(
                    (
                        row_node,
                        "MECHANISM",
                        node(f"ticket:{ticket}", "ticket"),
                        row.row_id,
                    )
                )
        if named_prs:
            for value in cells.get("check", []):
                for check in (c.strip() for c in value.split(",")):
                    if not is_real(check):
                        continue
                    check_node = node(f"check:{check}", "check")
                    edges.add((row_node, "NAMES_CHECK", check_node, row.row_id))
                    for pr in named_prs:
                        edges.add((check_node, "HOLDS_PR", f"pr:{pr}", row.row_id))

    graph_nodes = tuple(
        ModelWorkLedgerGraphNode(
            node_id=node_id, kind=kind, attrs=tuple(sorted(attrs.items()))
        )
        for node_id, (kind, attrs) in sorted(nodes.items())
    )
    graph_edges = tuple(
        ModelWorkLedgerGraphEdge(src=s, rel=r, dst=d, row_id=i)
        for s, r, d, i in sorted(edges)
    )
    return ModelWorkLedgerGraph(
        row_count=len(ordered),
        nodes=graph_nodes,
        edges=graph_edges,
        digest=graph_digest(graph_nodes, graph_edges, len(ordered)),
    )


def canonical_graph_bytes(
    nodes: Iterable[ModelWorkLedgerGraphNode],
    edges: Iterable[ModelWorkLedgerGraphEdge],
    row_count: int,
) -> bytes:
    payload = {
        "row_count": row_count,
        "nodes": [[n.node_id, n.kind, [list(a) for a in n.attrs]] for n in nodes],
        "edges": [[e.src, e.rel, e.dst, e.row_id] for e in edges],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def graph_digest(
    nodes: Iterable[ModelWorkLedgerGraphNode],
    edges: Iterable[ModelWorkLedgerGraphEdge],
    row_count: int,
) -> str:
    return hashlib.sha256(canonical_graph_bytes(nodes, edges, row_count)).hexdigest()


__all__ = [
    "canonical_graph_bytes",
    "derive_graph",
    "graph_digest",
    "is_real",
    "normalize_head",
    "normalize_install",
    "normalize_prs",
    "parse_cells",
    "tickets_of",
]
