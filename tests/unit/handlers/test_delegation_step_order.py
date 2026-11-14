# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation step's engine order: a flag, the overlay's order, or the old default (OMN-24044)."""

from __future__ import annotations

import pytest

from omnimarket.handlers import delegation_step_order as mod
from omnimarket.handlers.delegation_routing_order_reader import (
    DelegationRoutingOrderError,
)

pytestmark = pytest.mark.unit

LAB_FIRST = ("ladder", "claude-glm", "claude")
GLM_FIRST = ("claude-glm", "ladder")


def reader(order: tuple[str, ...] | None):
    asked: list[str] = []

    def read(task_class: str) -> tuple[str, ...] | None:
        asked.append(task_class)
        return order

    read.asked = asked  # type: ignore[attr-defined]
    return read


def test_flag_wins_and_the_reader_is_not_asked() -> None:
    read = reader(LAB_FIRST)
    answer = mod.step_order("text", "document", "glm", read=read)
    assert (answer.choice, answer.receipt_fields()) == ("glm", {"order_source": "flag"})
    assert read.asked == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("kind", "order", "choice"),
    [
        ("text", LAB_FIRST, "lab"),
        ("text", GLM_FIRST, "glm"),
        ("text", ("codex", "ladder"), "lab"),
        ("code", LAB_FIRST, "last"),
        ("code", ("ladder", "claude"), "last"),
        ("code", GLM_FIRST, "before-lab"),
    ],
)
def test_overlay_order_decides_when_no_flag(
    kind: str, order: tuple[str, ...], choice: str
) -> None:
    read = reader(order)
    answer = mod.step_order(kind, "document", read=read)
    assert answer.choice == choice
    assert answer.receipt_fields() == {
        "order_source": "routing_order_node",
        "routing_order": list(order),
    }
    assert read.asked == ["document"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("kind", "order", "old"),
    [
        ("text", None, "glm"),
        ("code", None, "before-lab"),
        ("text", ("claude", "codex"), "glm"),
        ("code", ("claude-glm", "claude"), "before-lab"),
    ],
)
def test_no_usable_order_keeps_the_old_default_and_says_why(
    kind: str, order: tuple[str, ...] | None, old: str
) -> None:
    answer = mod.step_order(kind, "research", read=reader(order))
    fields = answer.receipt_fields()
    assert answer.choice == old
    assert fields["order_source"] == "default"
    assert fields["order_fallback"]


def test_a_refused_reader_keeps_the_old_default_with_its_reason() -> None:
    def read(task_class: str) -> tuple[str, ...] | None:
        raise DelegationRoutingOrderError("overlay_invalid: bad chain")

    answer = mod.step_order("text", "document", read=read)
    assert answer.choice == "glm"
    assert "overlay_invalid: bad chain" in answer.receipt_fields()["order_fallback"]  # type: ignore[operator]


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        mod.step_order("review", "document", read=reader(None))
