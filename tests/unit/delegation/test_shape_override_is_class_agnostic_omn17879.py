# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17879: a declared response shape must reach EVERY task class, not one.

## Why this exists

24 FAILED `task_type: test` rows on the `.201` dev lane resolved `unconstrained`
while 28 otherwise-identical rows resolved `single_word`. Measured 2026-09-04
(`lakshman_ro`, READ ONLY, SELECT only), the two groups separate cleanly in time
with no interleaving:

    UNCONSTRAINED  24 rows  2026-08-10 04:28:40 -> 2026-08-31 05:44:56
    RESOLVED       28 rows  2026-08-31 13:45:03 -> 2026-09-03 05:57:57

and the SAME prompt string appears on both sides -- "Reply with the single word:
alive..." is 22 of the unconstrained rows and 27 of the resolved ones. The
resolver is a pure `re.search` over the prompt, and the `prompt=None` path is
unreachable from both production call sites, so the only input left that can
move the answer is the contract version inside the running image. The 24 are
historical: they predate the deploy carrying `34922a7c`, which introduced
`response_shape_directives`. Not a live defect.

## What this test pins, and why it is not about those 24 rows

`_declared_shape_directives` returns an EMPTY mapping when the contract declares
no `response_shape_directives`, and an empty mapping always resolves
`UNCONSTRAINED`. That is a deliberate no-op-on-old-contracts property and it is
already covered elsewhere.

The property at risk is different and forward-looking. Shape overrides resolve
class-first, then fall back to the contract-wide `default_shape_overrides`. So a
shape declared in `default_shape_overrides` reaches every task class at once --
which is exactly why `test` picked the override up without anyone editing the
`test` entry. If a future shape is instead landed as a per-class override on one
class, every OTHER class silently keeps its full prose rubric against a prompt
that declared a constrained answer, which is the OMN-17547 / OMN-17765 defect
re-created one class at a time.

The same shape was measured on the dev lane across `code_review`,
`complex_reasoning`, `planning`, `reasoning`, `review` and `documentation`
(OMN-17488), so "one class at a time" is the realistic failure, not a
hypothetical.

## Relationship to OMN-17765

OMN-17765 adds `definition_of_done.shape_overrides.<shape>.deterministic` to the
`test` class -- the first per-class block in this contract. These assertions are
written to hold in BOTH states: they check the HEURISTIC band only, and a class
block carrying just a `deterministic` key leaves the heuristic lookup falling
through to `default_shape_overrides` unchanged. If a future change adds a
per-class `heuristic` key that strands the other classes, that is what goes red
here.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _declared_shape_directives,
    _get_task_class_contract,
    _shape_override_heuristic,
    _task_class_entry,
)


def _contract() -> dict[str, object]:
    contract = _get_task_class_contract()
    assert isinstance(contract, dict), "task-class contract must load"
    return contract


def _declared_task_classes() -> list[str]:
    classes = _contract().get("task_classes")
    assert isinstance(classes, dict), "contract declares a task_classes mapping"
    assert classes, "task_classes is non-empty"
    return sorted(classes)


def _declared_shapes() -> list[EnumRequestedResponseShape]:
    """Every shape the contract declares a resolvable directive for.

    Driven off ``_declared_shape_directives``, which is the same read the
    resolver performs, so this covers a shape added tomorrow without anyone
    remembering to extend a list here.

    That helper keys its result by ``EnumRequestedResponseShape`` and skips
    contract names with no enum member. That skip is deliberate and documented
    at its definition -- the directive block is an open authoring surface, and a
    name the resolver can never return has no override to apply. So a
    contract-only name is dead weight rather than a stranding, and is
    out of scope here; the shapes that can actually reach a task class are
    exactly the ones below.
    """
    declared = _declared_shape_directives(_contract())
    shapes = [
        shape
        for shape, patterns in declared.items()
        if shape is not EnumRequestedResponseShape.UNCONSTRAINED and patterns
    ]
    assert shapes, "contract declares at least one resolvable response shape"
    return sorted(shapes, key=lambda s: s.value)


@pytest.mark.unit
def test_every_declared_shape_reaches_every_task_class() -> None:
    """No task class may be left behind by a declared shape override.

    Goes red the moment a shape is landed as a per-class `heuristic` override on
    one class instead of in `default_shape_overrides` -- which is the shape of
    OMN-17547 acceptance item 1 if it is scoped to `research` alone.
    """
    contract = _contract()
    stranded: list[str] = []

    for task_type in _declared_task_classes():
        entry = _task_class_entry(contract, task_type)
        for shape in _declared_shapes():
            if _shape_override_heuristic(contract, entry, shape) is None:
                stranded.append(f"{task_type} x {shape.value}")

    assert not stranded, (
        "these task-class/shape pairs resolve NO heuristic override, so a prompt "
        "declaring that shape is still graded against the full class rubric: "
        f"{stranded}. A shape belongs in `default_shape_overrides` so it reaches "
        "every class; a per-class `heuristic` override strands the rest."
    )


@pytest.mark.unit
def test_the_contract_wide_default_is_what_makes_it_class_agnostic() -> None:
    """Pin the mechanism, not just the outcome.

    The previous test would still pass if someone declared the same override on
    all seventeen classes by hand. That is not the property -- it would drift the
    moment class eighteen is added. What must hold is that the reach comes from
    `default_shape_overrides`.
    """
    defaults = _contract().get("default_shape_overrides")
    assert isinstance(defaults, dict), (
        "`default_shape_overrides` must exist: it is the only key that reaches a "
        "task class which declares no override of its own, and therefore the "
        "only one that covers a class added tomorrow."
    )
    assert defaults, "`default_shape_overrides` must be non-empty"
    for shape in _declared_shapes():
        block = defaults.get(shape.value)
        assert isinstance(block, dict), (
            f"shape {shape.value!r} is declared in `response_shape_directives` "
            f"but has no `default_shape_overrides` entry, so it reaches only the "
            f"classes that name it explicitly."
        )
        band = block.get("heuristic")
        assert isinstance(band, list), (
            f"`default_shape_overrides.{shape.value}.heuristic` must be a list; "
            f"an absent band silently leaves the class rubric in place rather "
            f"than replacing it."
        )
        assert band, (
            f"`default_shape_overrides.{shape.value}.heuristic` must be "
            f"non-empty; an empty band replaces the rubric with nothing."
        )


@pytest.mark.unit
def test_a_per_class_heuristic_override_does_not_strand_other_classes() -> None:
    """The negative case, exercised rather than described.

    Simulates OMN-17547 acceptance item 1 being landed as a `research`-only
    per-class heuristic override for a NEW shape, and asserts the class-agnostic
    check above would catch it. Without this, the first two tests pass today and
    nobody knows whether they would actually fail on the change they exist to
    block.

    Operates on a local copy of the contract dict; nothing is written and the
    real contract is untouched.
    """
    shape = EnumRequestedResponseShape.SINGLE_WORD
    entry_with_override: dict[str, object] = {
        "definition_of_done": {
            "shape_overrides": {shape.value: {"heuristic": ["no_refusal"]}}
        }
    }
    contract_without_default: dict[str, object] = {"task_classes": {}}

    # The class that names it resolves the override.
    assert (
        _shape_override_heuristic(contract_without_default, entry_with_override, shape)
        is not None
    )

    # Every other class -- one that names nothing -- is stranded, because the
    # contract-wide default is what it would otherwise have fallen back to.
    assert _shape_override_heuristic(contract_without_default, None, shape) is None, (
        "a class with no override of its own must fall back to "
        "`default_shape_overrides`; with none declared it resolves None, which "
        "is precisely the stranding this ticket exists to prevent"
    )
