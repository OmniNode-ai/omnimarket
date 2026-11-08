# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-22903): each of the five relations of the plan's cut table carries the join ids of the
piece owning each endpoint; an endpoint no piece owns reads as a finding."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.import_relations import (
    EnumRelationFindingKind,
    ModelImportRelationOverlay,
    ModelJoinDef,
    ModelLabelOwner,
)
from omnimarket.nodes.node_network_graph_compute.handlers.handler_import_relation_compute import (
    HandlerImportRelationCompute,
)

from .relation_support import bare_overlay, edge, example_overlay, request

pytestmark = pytest.mark.unit

_RUN = HandlerImportRelationCompute().handle

#: The five relations of the plan's cut table, source and target as labelled there.
FIVE = (
    ("dispatch", "ledger-session"),
    ("dispatch", "tickets-plans"),
    ("landing", "tickets-plans"),
    ("intelligence-memory", "seams-verify"),
    ("intelligence-memory", "landing"),
)


def _all_five_present() -> tuple:
    return tuple(edge(f"{s}:f.py", f"{t}:g.py") for s, t in FIVE)


def test_the_shipped_overlay_tracks_exactly_the_five_relations() -> None:
    tracked = [(k.source, k.target) for k in example_overlay().tracked_relations]
    assert tracked == list(FIVE)


def test_each_of_the_five_relations_carries_the_join_ids_of_both_endpoint_pieces() -> (
    None
):
    report = _RUN(request(_all_five_present(), example_overlay()))
    assert report.findings == ()
    assert report.tracked_total == 5
    assert report.tracked_mapped == 5
    by_relation = {
        (e.relation.source, e.relation.target): e for e in report.relation_map
    }
    dispatch_ledger = by_relation[("dispatch", "ledger-session")]
    assert (dispatch_ledger.source_piece, dispatch_ledger.target_piece) == (12, 6)
    assert dispatch_ledger.source_joins == ("J11", "J12")
    assert dispatch_ledger.target_joins == ("J5", "J6")
    landing_tickets = by_relation[("landing", "tickets-plans")]
    # Piece 9 sits on J8, J9, the held-out J-cycle and J-recover; piece 13 on J12 and J13.
    assert landing_tickets.source_joins == ("J-cycle", "J-recover", "J8", "J9")
    assert landing_tickets.target_joins == ("J12", "J13")
    assert all(
        e.source_joins and e.target_joins and e.mapped for e in report.relation_map
    )


def test_control_an_endpoint_no_piece_owns_reads_as_a_finding() -> None:
    owners = tuple(o for o in example_overlay().label_owners if o.label != "landing")
    overlay = example_overlay().model_copy(update={"label_owners": owners})
    report = _RUN(request(_all_five_present(), overlay))
    findings = [(f.relation.text(), f.kind) for f in report.findings]
    assert findings == [
        ("intelligence-memory to landing", EnumRelationFindingKind.UNMAPPED_ENDPOINT),
        ("landing to tickets-plans", EnumRelationFindingKind.UNMAPPED_ENDPOINT),
    ]
    assert report.tracked_mapped == 3
    unmapped = next(e for e in report.relation_map if e.relation.source == "landing")
    assert unmapped.source_piece is None
    assert not unmapped.mapped


def test_a_piece_with_no_join_reads_as_a_finding() -> None:
    overlay = ModelImportRelationOverlay.model_validate(
        {
            **bare_overlay(("x", "y"), owners={"x": 20, "y": 2}).model_dump(),
            "joins": [ModelJoinDef(join_id="J1", pieces=(1, 2)).model_dump()],
        }
    )
    report = _RUN(request((edge("x:a.py", "y:b.py"),), overlay))
    assert [f.kind for f in report.findings] == [
        EnumRelationFindingKind.PIECE_WITHOUT_JOIN
    ]
    assert not report.relation_map[0].mapped


def test_a_tracked_relation_the_graph_does_not_hold_reads_as_a_finding() -> None:
    """Under the old one-direction graph the reverse relation vanished: here that is a finding."""
    overlay = bare_overlay(("x", "y"), ("y", "x"), owners={"x": 1, "y": 2})
    edges = (
        edge("x:a.py", "y:b.py"),
        edge("x:c.py", "y:b.py"),
        edge("y:b.py", "x:a.py"),
    )
    kept = _RUN(request(edges, overlay))
    assert kept.findings == ()
    collapsed = _RUN(request(edges, overlay, keep_stronger_direction_only=True))
    assert [(f.relation.text(), f.kind) for f in collapsed.findings] == [
        ("y to x", EnumRelationFindingKind.NOT_IN_GRAPH)
    ]
    assert (
        next(e for e in collapsed.relation_map if e.relation.source == "y").occurrences
        == 0
    )


def test_every_owner_in_the_shipped_overlay_has_a_join() -> None:
    overlay = example_overlay()
    joined = {p for j in overlay.joins for p in j.pieces}
    assert {o.piece for o in overlay.label_owners} <= joined


def test_label_owner_model_refuses_a_piece_below_one() -> None:
    with pytest.raises(ValidationError):
        ModelLabelOwner(label="x", piece=0)
