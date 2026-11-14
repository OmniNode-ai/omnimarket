# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One host declaration, every other host table copy derived (OMN-21899).

Each hand fix of 2026-11-07 is a case here: a host left unavailable in two derived
copies after its owner row flipped, a column hand-added to one copy, and the flip
itself as the in-service action.
"""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from omnimarket.nodes.node_host_bringup_orchestrator.handlers.handler_host_declaration_derive import (
    HandlerHostDeclarationDerive,
)
from omnimarket.nodes.node_host_bringup_orchestrator.models.model_host_declaration import (
    EnumCopyDifferenceKind,
    ModelHostDeclarationDerived,
    ModelHostDeclarationDeriveRequest,
    ModelHostSurfaceSpec,
)

pytestmark = pytest.mark.unit

_SURFACES = (
    ModelHostSurfaceSpec(
        surface="packaged", fields=("target", "available", "unavailable_reason", "cap")
    ),
    ModelHostSurfaceSpec(
        surface="overlays-repo", fields=("target", "available", "unavailable_reason")
    ),
)
_DECL: tuple[dict[str, JsonValue], ...] = (
    {"name": "ha", "target": "ha", "cap": 4},
    {
        "name": "hb",
        "target": "hb",
        "available": False,
        "unavailable_reason": "repair",
        "cap": 8,
    },
)


def _derive(**kw: object) -> ModelHostDeclarationDerived:
    fields: dict[str, object] = {"declaration": _DECL, "surfaces": _SURFACES}
    fields.update(kw)
    return HandlerHostDeclarationDerive().handle(
        ModelHostDeclarationDeriveRequest.model_validate(fields)
    )


def test_copies_equal_to_the_derivation_are_in_sync() -> None:
    first = _derive()
    again = _derive(copies=first.derived)

    assert again.in_sync
    assert again.differences == ()
    assert first.derived["overlays-repo"][0] == {"name": "ha", "target": "ha"}


def test_flip_is_the_in_service_action_and_every_copy_follows() -> None:
    result = _derive(flip_in_service=("hb",))

    hb = next(r for r in result.declaration if r["name"] == "hb")
    assert "available" not in hb
    assert "unavailable_reason" not in hb
    for rows in result.derived.values():
        assert all("available" not in r for r in rows)


def test_host_left_unavailable_in_two_copies_names_each_copy() -> None:
    """2026-11-07: the owner row was available, two copies still marked it out."""
    stale = _derive().derived
    result = _derive(flip_in_service=("hb",), copies=stale)

    assert not result.in_sync
    named = {(d.surface, d.host, d.field, d.kind) for d in result.differences}
    assert (
        "packaged",
        "hb",
        "available",
        EnumCopyDifferenceKind.SURPLUS_FIELD,
    ) in named
    assert (
        "overlays-repo",
        "hb",
        "available",
        EnumCopyDifferenceKind.SURPLUS_FIELD,
    ) in named


def test_hand_added_column_in_one_copy_is_named() -> None:
    """2026-11-07: a column added by hand to one copy broke placement."""
    copies = dict(_derive().derived)
    copies["packaged"] = tuple(
        {**r, "volumes_root": "/data"} if r["name"] == "ha" else r
        for r in copies["packaged"]
    )
    result = _derive(copies=copies)

    assert [(d.surface, d.host, d.field, d.kind) for d in result.differences] == [
        ("packaged", "ha", "volumes_root", EnumCopyDifferenceKind.SURPLUS_FIELD)
    ]
    assert result.differences[0].have == '"/data"'


def test_value_drift_names_both_values() -> None:
    copies = dict(_derive().derived)
    copies["packaged"] = tuple(
        {**r, "cap": 6} if r["name"] == "ha" else r for r in copies["packaged"]
    )
    (diff,) = _derive(copies=copies).differences

    assert (diff.kind, diff.have, diff.want) == (
        EnumCopyDifferenceKind.VALUE_DIFFERS,
        "6",
        "4",
    )


def test_missing_and_surplus_hosts_are_named() -> None:
    copies = dict(_derive().derived)
    copies["overlays-repo"] = (
        {"name": "ha", "target": "ha"},
        {"name": "hz", "target": "hz"},
    )
    named = {(d.host, d.kind) for d in _derive(copies=copies).differences}

    assert named == {
        ("hb", EnumCopyDifferenceKind.MISSING_HOST),
        ("hz", EnumCopyDifferenceKind.SURPLUS_HOST),
    }


def test_flip_of_an_undeclared_host_is_refused() -> None:
    result = _derive(flip_in_service=("hq",))

    assert result.refusals == (
        "flip of hq refused: the declaration holds no such host",
    )
    assert not result.in_sync


def test_a_copy_of_an_undeclared_surface_is_refused() -> None:
    result = _derive(copies={"handmade": ()})

    assert result.refusals
    assert "handmade" in result.refusals[0]


def test_copy_holding_one_host_twice_names_the_duplicate_even_when_the_last_row_matches() -> (
    None
):
    """Break: a stale first row of ``ha`` beside a correct last row; the last row alone matches the derivation."""
    derived = _derive().derived
    stale = {"name": "ha", "target": "ha", "cap": 1}
    copies = {**derived, "packaged": (stale, *derived["packaged"])}
    result = _derive(copies=copies)

    assert not result.in_sync
    named = {(d.surface, d.host, d.kind) for d in result.differences}
    assert ("packaged", "ha", EnumCopyDifferenceKind.DUPLICATE_HOST) in named
