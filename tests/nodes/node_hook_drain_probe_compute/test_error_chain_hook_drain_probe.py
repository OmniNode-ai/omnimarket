# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: the bound, the same-class stall and the failing control (OMN-19998)."""

from __future__ import annotations

from collections.abc import Callable, Collection
from itertools import product

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_hook_drain_probe_compute.handlers import (
    HandlerHookDrainProbe,
)
from omnimarket.nodes.node_hook_drain_probe_compute.models import (
    ModelHookDrainProbeRequest,
)


def order(
    pending: tuple[str, ...], refused: tuple[str, ...] = (), max_classes: int = 3
) -> tuple[int, ...]:
    return (
        HandlerHookDrainProbe()
        .handle(
            ModelHookDrainProbeRequest(
                pending_event_types=pending,
                refused_event_types=refused,
                max_classes=max_classes,
            )
        )
        .probe_order
    )


def drain_probe(
    pending: tuple[str, ...],
    accepts: Callable[[str], bool],
    refused: Collection[str] = (),
    max_classes: int = 3,
) -> int | None:
    """What the drainer does with the order: publish in turn, the first accepted record proves the broker."""
    for index in order(pending, tuple(refused), max_classes):
        if accepts(pending[index]):
            return index
    return None


def old_single_class_probe(
    pending: tuple[str, ...], accepts: Callable[[str], bool]
) -> int | None:
    """The probe before this node: the first different class, else the record directly behind, asked once."""
    if len(pending) < 2:
        return None
    candidate = next((i for i in range(1, len(pending)) if pending[i] != pending[0]), 1)
    return candidate if accepts(pending[candidate]) else None


H053 = (
    "prompt.submitted",
    "content.captured",
    "hook.event",
    "hook.event",
    "prompt.submitted",
)


def broker_refusing(*classes: str) -> Callable[[str], bool]:
    return lambda event_type: event_type not in classes


def test_failing_control_the_single_class_probe_misses_the_authorized_records() -> None:
    """The h053 reading: the old probe asked content.captured only, read it as an unreachable broker, moved nothing."""
    accepts = broker_refusing("prompt.submitted", "content.captured")
    assert old_single_class_probe(H053, accepts) is None
    assert drain_probe(H053, accepts) == 2


def test_fewer_than_two_pending_has_no_probe() -> None:
    assert order(()) == ()
    assert order(("prompt.submitted",)) == ()


def test_the_probe_stops_at_the_bound_so_a_dead_broker_pays_a_bounded_cost() -> None:
    pending = ("head", "a", "b", "c", "d", "ok")
    assert order(pending, max_classes=3) == (1, 2, 3)
    assert drain_probe(pending, broker_refusing("head", "a", "b", "c", "d")) is None


def test_a_whole_batch_of_the_heads_class_probes_the_record_directly_behind() -> None:
    assert order(("x", "x", "x")) == (1,)
    assert drain_probe(("x", "x", "x"), lambda _t: False) is None


def test_when_every_other_class_is_already_refused_the_record_directly_behind_is_the_probe() -> (
    None
):
    assert order(("a", "b", "b"), refused=("b",)) == (1,)


def test_one_record_per_class_never_two_of_a_class() -> None:
    assert order(("a", "b", "b", "c", "b", "c")) == (1, 3)


def test_the_request_is_closed_and_bounded() -> None:
    with pytest.raises(ValidationError):
        ModelHookDrainProbeRequest(pending_event_types=("a",), max_classes=0)
    with pytest.raises(ValidationError):
        ModelHookDrainProbeRequest.model_validate(
            {"pending_event_types": ("a",), "surprise": 1}
        )


def reference_probe(
    pending: tuple[str, ...],
    accepts: Callable[[str], bool],
    refused: Collection[str],
    limit: int,
) -> int | None:
    """The drainer's in-line probe as written before the node (omniclaude drainer-probe-classes), the oracle."""
    if len(pending) < 2:
        return None
    head = pending[0]
    skip = set(refused)
    asked: list[str] = []
    for i in range(1, len(pending)):
        t = pending[i]
        if t == head or t in skip or t in asked:
            continue
        asked.append(t)
        if accepts(t):
            return i
        if len(asked) >= limit:
            return None
    if asked:
        return None
    return 1 if accepts(pending[1]) else None


def test_the_node_answers_as_the_in_line_probe_did_for_every_small_batch() -> None:
    classes = ("a", "b", "c")
    checked = 0
    for length in range(0, 7):
        for pending in product(classes, repeat=length):
            for denied in product((False, True), repeat=3):
                refusing = {c for c, d in zip(classes, denied, strict=True) if d}
                for skipped in product((False, True), repeat=3):
                    skip = tuple(c for c, s in zip(classes, skipped, strict=True) if s)
                    accepts = broker_refusing(*refusing)
                    for limit in (1, 2, 3):
                        assert drain_probe(pending, accepts, skip, limit) == (
                            reference_probe(pending, accepts, skip, limit)
                        ), (pending, refusing, skip, limit)
                        checked += 1
    assert checked > 40000
