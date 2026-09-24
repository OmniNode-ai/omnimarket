# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The live contract's class set, as ``onex delegate --task-type`` admits it (OMN-13966).

``onex delegate`` checks an explicit ``--task-type`` against every class this
contract declares, public and internal alike, and refuses only a class the
contract itself declares unroutable with a ``routing_availability`` block,
quoting that block. Before OMN-13966 it checked the public projection only and
refused the four internal classes as ``unknown task type``.

THE OTHER HALF OF THIS ASSERTION LIVES IN OMNIBASE_INFRA:
``tests/unit/cli/test_task_class_admission_omn13966.py`` pins the CLI's
admission to the same two lists, against a stand-in contract that carries the
whole class set. Neither suite can import the other's half (layering one way,
a registry pin the other, and omnibase_infra's venv-purity gate refuses to run
with omnimarket installed), so the seam is two pinned halves, the same shape
as the public-vocabulary pin in ``test_task_class_selection_omn18305.py``.

A class added to or removed from this contract, or a class gaining or losing a
``routing_availability`` block, turns THIS test red first. The fix is to update
both lists here and in omnibase_infra, and the stand-in contract there, in the
same change; the omnibase_infra half then proves the CLI admits exactly the new
partition.
"""

from __future__ import annotations

import pytest

from omnimarket.inference.task_class_authority import load_task_class_authority

pytestmark = pytest.mark.unit

#: Every declared class ``--task-type`` must admit by explicit name.
_EXPECTED_ADMITTED_CLASSES = (
    "code_generation",
    "code_review",
    "complex_reasoning",
    "document",
    "documentation",
    "escalation",
    "planning",
    "reasoning",
    "refactor",
    "research",
    "review",
    "summarization",
    "test",
    "validator_generation",
)

#: Every declared class the contract marks unroutable, which the CLI refuses.
_EXPECTED_UNAVAILABLE_CLASSES = ("agent_delegation",)

#: The block fields the CLI quotes in its refusal; a block missing one fails closed there.
_QUOTED_FIELDS = ("status", "missing_capability", "tracking", "reason")


def _routing_availability() -> dict[str, object]:
    """Map each class declaring ``routing_availability`` to its block."""
    authority = load_task_class_authority()
    declared: dict[str, object] = {}
    for name, entry in authority.task_classes.items():
        extra = entry.model_extra or {}
        if "routing_availability" in extra:
            declared[name] = extra["routing_availability"]
    return declared


class TestTheContractPartitionMatchesTheCliPin:
    def test_the_declared_class_set_is_the_pinned_partition(self) -> None:
        authority = load_task_class_authority()
        admitted = set(_EXPECTED_ADMITTED_CLASSES)
        unavailable = set(_EXPECTED_UNAVAILABLE_CLASSES)
        assert admitted.isdisjoint(unavailable)
        assert set(authority.task_classes) == admitted | unavailable

    def test_the_unroutable_classes_are_exactly_the_pinned_ones(self) -> None:
        assert sorted(_routing_availability()) == sorted(_EXPECTED_UNAVAILABLE_CLASSES)

    def test_every_unroutable_block_carries_the_fields_the_cli_quotes(self) -> None:
        for name, block in _routing_availability().items():
            assert isinstance(block, dict), name
            missing = [field for field in _QUOTED_FIELDS if not block.get(field)]
            assert missing == [], f"{name}.routing_availability lacks {missing}"

    def test_every_admitted_class_declares_an_execution_budget(self) -> None:
        """The CLI reads the chosen class's budget next; an admitted class without one refuses there."""
        authority = load_task_class_authority()
        assert set(_EXPECTED_ADMITTED_CLASSES) <= set(authority.execution_budgets)
