# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17907 AC4: the "never empty" deterministic band is pinned by a test.

`_shape_override_deterministic` raises on a declared-but-empty (or non-list)
`deterministic` band, because an empty deterministic band reports a degraded
verdict as complete truth. That behaviour is OMN-17765's C2 constraint and until
now it rested on the reader's eye -- no test referenced the helper at all.

Measured on `dev` at 47d63119 before this file existed:

    grep -rn "_shape_override_deterministic" tests/   ->  no matches

The gap is not theoretical. Injecting `deterministic: []` into the committed
contract and running the gates that fire on that file:

    OMN-15630 routing-completeness hook   21 passed   (does not see it)
    resolver at request time              raises      (fail-closed, correct)
    test_..._records_which_contract_key   1 failed    (INCIDENTAL -- that test
                                                       asserts band provenance;
                                                       the raise merely escapes)

So a committed empty band is caught today only by a test checking something
else. This file makes the check direct, so the property survives a refactor of
the test that currently catches it by accident.

The negative half matters as much as the raise: a test that only pins the raise
would pass an implementation that raised on everything, and "omit the key
entirely to keep the class band" is the documented way to decline an override.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _shape_override_deterministic,
)

_SHAPE = EnumRequestedResponseShape.SINGLE_WORD


def _entry(deterministic: object, *, declare: bool = True) -> dict[str, object]:
    """A task-class entry declaring (or omitting) a deterministic band."""
    for_shape: dict[str, object] = {"heuristic": ["no_refusal"]}
    if declare:
        for_shape["deterministic"] = deterministic
    return {"definition_of_done": {"shape_overrides": {_SHAPE.value: for_shape}}}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("declared", "case"),
    [
        ([], "empty list"),
        ({}, "empty mapping"),
        ("", "empty string"),
        ("response_non_empty", "bare string, not a list"),
        ([1, 2], "list with no strings"),
        ([None], "list of a single non-string"),
        (None, "explicit null"),
    ],
)
def test_a_declared_but_empty_deterministic_band_raises(
    declared: object, case: str
) -> None:
    """OMN-17765 C2: an empty band would present a degraded verdict as truth.

    Every case here resolves to zero usable string checks. Declaring the key and
    resolving it to nothing is the defect; the raise is the fail-closed.
    """
    with pytest.raises(ValueError, match="declared but empty or not a list"):
        _shape_override_deterministic(_entry(declared), _SHAPE)


@pytest.mark.unit
def test_omitting_the_key_entirely_does_not_raise() -> None:
    """The negative half — without it, an implementation that raises on
    everything would pass the test above.

    The docstring's own remedy is "omit the key entirely to keep the class
    band", so omission must resolve None (not declared, floor untouched) rather
    than raising. `None` is a different fact from an empty band and the code
    treats it as one.
    """
    assert _shape_override_deterministic(_entry(None, declare=False), _SHAPE) is None


@pytest.mark.unit
def test_a_well_formed_band_resolves_and_drops_non_strings() -> None:
    """A valid declaration still resolves, and mixed content keeps its strings.

    Pins that the raise is about resolving to *nothing*, not about strictness:
    a list carrying at least one string is usable and is returned filtered.
    """
    assert _shape_override_deterministic(
        _entry(["response_non_empty", "final_artifact_only"]), _SHAPE
    ) == ("response_non_empty", "final_artifact_only")
    assert _shape_override_deterministic(_entry(["response_non_empty", 7]), _SHAPE) == (
        "response_non_empty",
    )


@pytest.mark.unit
def test_an_unconstrained_shape_never_reaches_the_raise() -> None:
    """UNCONSTRAINED returns before the band is read, even on a broken entry.

    Guards the ordering: were the early return removed, every no-shape request
    against a class carrying a malformed override would start raising in
    production.

    The entry must declare the override under UNCONSTRAINED's own key. Keying it
    on any other shape makes this vacuous -- the lookup would miss and return
    None for the wrong reason, and the test would pass with the early return
    deleted. Measured: it did exactly that before this was corrected.
    """
    broken_under_unconstrained: dict[str, object] = {
        "definition_of_done": {
            "shape_overrides": {
                EnumRequestedResponseShape.UNCONSTRAINED.value: {"deterministic": []}
            }
        }
    }
    assert (
        _shape_override_deterministic(
            broken_under_unconstrained, EnumRequestedResponseShape.UNCONSTRAINED
        )
        is None
    )
