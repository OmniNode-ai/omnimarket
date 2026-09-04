# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17765: a declared short-form shape must not carry a test-artifact floor.

Measured on the `.201` dev lane 2026-09-04: 52 terminal FAILED correlations on
`task_type: test` were rejected for `missing @pytest.mark.unit`, and 0 of the 52
asked for test code. Every one is a liveness probe -- 49 of them the literal
string "Reply with the single word: alive. This is an automated liveness probe".

`resolve_task_class_dod_checks` already read the prompt and already replaced the
HEURISTIC band when the prompt declared a response shape (OMN-16932), but returned
the deterministic band unchanged on every path -- so a prompt saying "reply with
one word" still resolved `uses_pytest_mark_unit`, a marker the requested answer
cannot carry by construction. The gate then reported "TASK_MISMATCH: missing
@pytest.mark.unit", which is not merely strict but false: a one-word reply to a
one-word request does not mismatch its task.

The fix is a contract-declared `shape_overrides.<shape>.deterministic` sibling to
the existing `heuristic` key. Keying on the prompt is legitimate here: the prompt
is the CALLER's, and the constraint at `handler_quality_gate.py:733` forbids
selecting an override from the RESPONSE's shape, which the model controls. The
gate cannot see the prompt at all.

The five guard tests matter as much as the failing one, and each blocks a
different wrong fix:

- a genuine test ask still resolves the marker -- blocks a blanket removal
- the short-form heuristic override still resolves -- pins OMN-16932 untouched,
  and pins the fallthrough to `default_shape_overrides` for the heuristic band
- `prompt=None` keeps the class band -- pins the no-prompt caller path
- a shape directive cannot drop `code_generation`'s floor -- blocks the sibling
  becoming a general caller-controlled bypass, and goes red if the key is ever
  moved into `default_shape_overrides`
- both resolution sites agree -- the deterministic band was chosen in TWO places,
  and a fix landing only in this one would have gone green while the bus reducer
  (the path that produced the measured rows) stayed unchanged
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

_SHORT_FORM_PROMPT = (
    "Reply with the single word: alive. This is an automated liveness probe"
)
_TEST_CODE_PROMPT = "Write a pytest unit test asserting that an empty list is falsy."


@pytest.mark.unit
def test_short_form_prompt_does_not_resolve_the_pytest_marker_floor() -> None:
    """A prompt declaring a one-word answer must not demand a pytest marker."""
    deterministic, _ = resolve_task_class_dod_checks("test", prompt=_SHORT_FORM_PROMPT)
    assert "uses_pytest_mark_unit" not in deterministic


@pytest.mark.unit
def test_genuine_test_ask_still_resolves_the_pytest_marker_floor() -> None:
    """The floor is a property of a test artifact and must survive for one."""
    deterministic, _ = resolve_task_class_dod_checks("test", prompt=_TEST_CODE_PROMPT)
    assert "uses_pytest_mark_unit" in deterministic


@pytest.mark.unit
def test_short_form_prompt_still_resolves_the_heuristic_override() -> None:
    """OMN-16932's heuristic override must be unaffected by this change."""
    _, heuristic = resolve_task_class_dod_checks("test", prompt=_SHORT_FORM_PROMPT)
    assert "short_form_adequacy" in heuristic


@pytest.mark.unit
def test_prompt_none_leaves_the_class_definition_of_done_unchanged() -> None:
    """A caller with no prompt keeps the declared band, per OMN-16932."""
    deterministic, _ = resolve_task_class_dod_checks("test", prompt=None)
    assert "uses_pytest_mark_unit" in deterministic


@pytest.mark.unit
def test_a_shape_directive_cannot_drop_the_code_generation_floor() -> None:
    """The sibling narrows a declared class; it is not a general bypass.

    `code_generation` declares `compiles_without_errors` and no
    `shape_overrides`. A caller who prefixes a shape directive to a code request
    must not thereby drop that floor -- if they could, the deterministic band
    would be caller-controlled rather than contract-declared, which is the whole
    thing the per-class limit exists to prevent.

    This goes red if anyone later moves the `deterministic` key up into
    `default_shape_overrides`, which applies to every class at once.
    """
    deterministic, _ = resolve_task_class_dod_checks(
        "code_generation",
        prompt="Reply with the single word: alive. This is an automated liveness probe",
    )
    assert "compiles_without_errors" in deterministic


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_type", "prompt"),
    [
        ("test", _SHORT_FORM_PROMPT),
        ("test", _TEST_CODE_PROMPT),
        ("code_generation", _SHORT_FORM_PROMPT),
    ],
)
def test_both_resolution_sites_agree_on_the_same_input(
    task_type: str, prompt: str
) -> None:
    """The bus reducer and the local dispatch path must resolve identically.

    The deterministic band used to be chosen in two places: this function, whose
    only production caller is the bus-LESS local dispatch path, and the bus
    routing reducer, which re-derived it and applied the shape override to the
    heuristic band only. The reducer is the path that produced the measured
    rows, so a fix landing only here would have gone green while the lane was
    unchanged -- a false green on the ticket filed to catch false greens.

    The reducer now calls this resolver, so the two agree by construction. This
    asserts the property directly against the reducer's own resolution so the
    seam cannot silently reopen if someone re-inlines it.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
        _definition_of_done_checks,
        _get_task_class_contract,
        _shape_override_deterministic,
        _shape_override_heuristic,
        _task_class_entry,
        resolve_requested_shape_for_prompt,
    )

    shared_deterministic, shared_heuristic = resolve_task_class_dod_checks(
        task_type, prompt
    )

    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    reducer_deterministic, reducer_heuristic = _definition_of_done_checks(entry)
    shape = resolve_requested_shape_for_prompt(prompt)
    heuristic_override = _shape_override_heuristic(contract, entry, shape)
    if heuristic_override is not None:
        reducer_heuristic = heuristic_override
    deterministic_override = _shape_override_deterministic(entry, shape)
    if deterministic_override is not None:
        reducer_deterministic = deterministic_override

    assert shared_deterministic == reducer_deterministic
    assert shared_heuristic == reducer_heuristic
