# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation step's engine order: a flag, the overlay's order, or the old default (OMN-24044, OMN-24507).

A ``text`` step reads the routing chain through the reader and runs on the first engine the chain places that the host
has; a ``code`` step reads the step order overlay's ``code_engine_order`` and never reads the chain. A reader or
overlay that refuses, and an order no engine is left on, raise
:class:`~omnimarket.handlers.delegation_step_order.DelegationStepOrderRefusalError`, never a default.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omnimarket.enums.enum_delegation_step_order_refusal import (
    EnumDelegationStepOrderRefusal,
)
from omnimarket.handlers import delegation_step_order as mod
from omnimarket.handlers.delegation_routing_order_reader import (
    DelegationRoutingOrderError,
)
from omnimarket.models.model_delegation_step_order_overlay import (
    ModelDelegationStepOrderOverlay,
)

pytestmark = pytest.mark.unit

LAB_FIRST = ("ladder", "claude-glm", "claude")
GLM_FIRST = ("claude-glm", "ladder")


class Reader:
    """A routing-order reader that records the task classes it was asked."""

    def __init__(self, order: tuple[str, ...] | None) -> None:
        self.order = order
        self.asked: list[str] = []

    def __call__(self, task_class: str) -> tuple[str, ...] | None:
        self.asked.append(task_class)
        return self.order


def reader(order: tuple[str, ...] | None) -> Reader:
    return Reader(order)


def no_overlay() -> ModelDelegationStepOrderOverlay | None:
    """An install with no delegation step order overlay."""
    return None


def step_overlay(
    order: tuple[str, ...], programs: dict[str, str | None]
) -> Callable[[], ModelDelegationStepOrderOverlay]:
    """An install whose overlay orders ``order`` and names each engine's program."""
    value = ModelDelegationStepOrderOverlay(
        code_engine_order=order, engine_programs=programs
    )
    return lambda: value


def test_flag_wins_and_the_reader_is_not_asked() -> None:
    read = reader(LAB_FIRST)
    answer = mod.step_order("text", "document", "glm", read=read)
    assert (answer.choice, answer.receipt_fields()) == ("glm", {"order_source": "flag"})
    assert read.asked == []


@pytest.mark.parametrize(
    ("order", "choice"),
    [
        (LAB_FIRST, "lab"),
        (GLM_FIRST, "glm"),
        (("codex", "ladder"), "lab"),
    ],
)
def test_the_routing_order_decides_a_text_step_when_no_flag(
    order: tuple[str, ...], choice: str
) -> None:
    read = reader(order)
    answer = mod.step_order("text", "document", read=read, overlay=no_overlay)
    assert answer.choice == choice
    assert answer.receipt_fields() == {
        "order_source": "routing_order_node",
        "routing_order": list(order),
    }
    assert read.asked == ["document"]


@pytest.mark.parametrize(
    ("order", "choice"),
    [
        (("lab", "glm"), "last"),
        (("glm", "lab"), "before-lab"),
        (("lab",), "last"),
        (("glm",), "before-lab"),
    ],
)
def test_the_overlay_decides_a_code_step_when_no_flag(
    order: tuple[str, ...], choice: str
) -> None:
    answer = mod.step_order(
        "code",
        "document",
        overlay=step_overlay(order, {"lab": None, "glm": "claude"}),
        has_program=lambda _name: True,
    )
    assert answer.choice == choice
    assert answer.receipt_fields() == {
        "order_source": "overlay",
        "routing_order": list(order),
    }


@pytest.mark.parametrize(
    ("kind", "old"),
    [
        ("text", "glm"),
        ("code", "before-lab"),
    ],
)
def test_no_usable_order_keeps_the_old_default_and_says_why(
    kind: str, old: str
) -> None:
    answer = mod.step_order(kind, "research", read=reader(None), overlay=no_overlay)
    fields = answer.receipt_fields()
    assert answer.choice == old
    assert fields["order_source"] == "default"
    assert fields["order_fallback"]


def test_a_refused_reader_refuses_the_step_order() -> None:
    def read(task_class: str) -> tuple[str, ...] | None:
        raise DelegationRoutingOrderError("overlay_invalid: bad chain")

    with pytest.raises(mod.DelegationStepOrderRefusalError) as caught:
        mod.step_order("text", "document", read=read, overlay=no_overlay)
    assert caught.value.refusal is EnumDelegationStepOrderRefusal.READER_REFUSED
    assert "overlay_invalid: bad chain" in caught.value.detail


def test_an_order_that_places_no_engine_refuses_the_step_order() -> None:
    with pytest.raises(mod.DelegationStepOrderRefusalError) as caught:
        mod.step_order(
            "text", "document", read=reader(("claude", "codex")), overlay=no_overlay
        )
    assert caught.value.refusal is EnumDelegationStepOrderRefusal.ENGINES_NOT_PLACED


def test_the_first_text_engine_is_skipped_when_its_program_is_absent() -> None:
    answer = mod.step_order(
        "text",
        "document",
        read=reader(LAB_FIRST),
        overlay=step_overlay(
            ("lab", "glm"), {"lab": "omn-no-such-program-24507", "glm": None}
        ),
        has_program=lambda name: name != "omn-no-such-program-24507",
    )
    assert answer.choice == "glm"
    assert answer.receipt_fields() == {
        "order_source": "routing_order_node",
        "routing_order": list(LAB_FIRST),
        "engines_absent": ["lab"],
    }


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        mod.step_order("review", "document", read=reader(None))
