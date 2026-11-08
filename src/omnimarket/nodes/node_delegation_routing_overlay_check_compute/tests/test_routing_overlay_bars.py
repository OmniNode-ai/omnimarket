# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-task-class bars in the routing overlay, frozen with a hash (rebuild piece 15, OMN-22976).

Every test that names a class runs once per task class of the shipped contract, so a
new class cannot ship without its own bar and its own passing and failing reads.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_routing_overlay_check_compute.models.model_routing_overlay_check import (
    EnumOverlayFindingClass as Finding,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.models.model_task_class_bar import (
    ModelTaskClassBar,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.tests.support import (
    bar_body,
    bars,
    check,
    class_names,
    contract_class,
    contracts,
    findings,
    other_required_bar,
    overlay,
    refreeze,
)

pytestmark = pytest.mark.unit

CLASSES = class_names()


def test_shipped_overlay_passes_over_every_class() -> None:
    result = check()
    assert result.findings == ()
    assert result.ok is True
    assert result.checked_classes == len(CLASSES) > 0


@pytest.mark.parametrize("task_class", CLASSES)
def test_every_class_has_bar_in_the_shipped_overlay(task_class: str) -> None:
    slot = bars(overlay())[task_class]
    bar = ModelTaskClassBar.model_validate(slot["bar"])
    assert slot["bar_hash"] == bar.frozen_hash(task_class)
    assert len(str(slot["bar_hash"])) == 64


@pytest.mark.parametrize("task_class", CLASSES)
def test_every_class_has_bar_a_class_without_one_fails(task_class: str) -> None:
    doc = overlay()
    del bars(doc)[task_class]
    result = check(overlay_doc=doc)
    assert result.ok is False
    assert findings(result) == {(task_class, Finding.BAR_MISSING)}


def test_every_class_has_bar_an_empty_overlay_fails_every_class() -> None:
    result = check(overlay_doc={"task_class_bars": {}})
    assert (
        "<overlay>",
        Finding.OVERLAY_INVALID,
    ) in findings(result)
    assert {(c, Finding.BAR_MISSING) for c in CLASSES} <= findings(result)


def test_every_class_has_bar_a_new_contract_class_fails_until_it_has_one() -> None:
    contract_doc = contracts()
    classes = contract_doc["task_classes"]
    assert isinstance(classes, dict)
    classes["brand_new_class"] = contract_class(contract_doc, CLASSES[0])
    result = check(contract_doc=contract_doc)
    assert findings(result) == {("brand_new_class", Finding.BAR_MISSING)}


def test_every_class_has_bar_an_overlay_bar_for_no_class_fails() -> None:
    doc = overlay()
    bars(doc)["ghost_class"] = bars(doc)[CLASSES[0]]
    assert findings(check(overlay_doc=doc)) == {("ghost_class", Finding.UNKNOWN_CLASS)}


@pytest.mark.parametrize("task_class", CLASSES)
def test_hash_change_fails_a_bar_edited_without_a_new_hash(task_class: str) -> None:
    doc = overlay()
    body = bar_body(doc, task_class)
    body["required_bar"] = other_required_bar(float(str(body["required_bar"])))
    result = check(overlay_doc=doc)
    assert result.ok is False
    assert (task_class, Finding.HASH_MISMATCH) in findings(result)


@pytest.mark.parametrize("task_class", CLASSES)
def test_hash_change_fails_a_contract_bar_moved_without_refreezing(
    task_class: str,
) -> None:
    contract_doc = contracts()
    gate = contract_class(contract_doc, task_class)["quality_gate"]
    assert isinstance(gate, dict)
    gate["required_bar"] = other_required_bar(float(gate["required_bar"]))
    result = check(contract_doc=contract_doc)
    assert result.ok is False
    assert findings(result) == {(task_class, Finding.BAR_DRIFT)}


@pytest.mark.parametrize("task_class", CLASSES)
def test_hash_change_fails_a_hash_copied_from_another_class(task_class: str) -> None:
    doc = overlay()
    donor = next(c for c in CLASSES if c != task_class)
    bars(doc)[task_class]["bar"] = bars(doc)[donor]["bar"]
    bars(doc)[task_class]["bar_hash"] = bars(doc)[donor]["bar_hash"]
    assert (task_class, Finding.HASH_MISMATCH) in findings(check(overlay_doc=doc))


@pytest.mark.parametrize("task_class", CLASSES)
def test_a_bar_refrozen_with_its_new_hash_passes(task_class: str) -> None:
    """Control: a deliberate change, bar and hash rewritten together, is the allowed path."""
    contract_doc = contracts()
    gate = contract_class(contract_doc, task_class)["quality_gate"]
    assert isinstance(gate, dict)
    new_bar = other_required_bar(float(gate["required_bar"]))
    gate["required_bar"] = new_bar
    doc = overlay()
    bar_body(doc, task_class)["required_bar"] = new_bar
    refreeze(doc, task_class)
    assert check(contract_doc=contract_doc, overlay_doc=doc).findings == ()


@pytest.mark.parametrize("task_class", CLASSES)
def test_hash_change_fails_unless_the_shipped_bar_hashes_to_its_hash(
    task_class: str,
) -> None:
    """The shipped file itself: a bar edited in it without a new hash turns this red."""
    result = check()
    assert not [f for f in result.findings if f.task_class == task_class]


def test_hash_change_fails_a_missing_or_short_hash() -> None:
    doc = overlay()
    del bars(doc)[CLASSES[0]]["bar_hash"]
    bars(doc)[CLASSES[1]]["bar_hash"] = str(bars(doc)[CLASSES[1]]["bar_hash"])[:16]
    assert findings(check(overlay_doc=doc)) == {
        (CLASSES[0], Finding.HASH_MALFORMED),
        (CLASSES[1], Finding.HASH_MALFORMED),
    }


def test_a_malformed_bar_is_refused() -> None:
    doc = overlay()
    bars(doc)[CLASSES[0]]["bar"] = {"required_bar": 0.8}
    assert findings(check(overlay_doc=doc)) == {(CLASSES[0], Finding.BAR_MALFORMED)}


def test_a_contract_class_with_no_complete_bar_is_refused() -> None:
    contract_doc = contracts()
    del contract_class(contract_doc, CLASSES[0])["latency_sla_p99_ms"]
    assert (CLASSES[0], Finding.CONTRACT_BAR_MISSING) in findings(
        check(contract_doc=contract_doc)
    )


def test_a_non_string_class_key_in_the_contract_is_a_finding_not_a_crash() -> None:
    """Tessl finding (OMN-23064): a YAML key like ``1`` must not escape as ValidationError."""
    contract_doc = contracts()
    classes = contract_doc["task_classes"]
    assert isinstance(classes, dict)
    classes[1] = contract_class(contract_doc, CLASSES[0])
    result = check(contract_doc=contract_doc)
    assert result.ok is False
    assert ("<overlay>", Finding.OVERLAY_INVALID) in findings(result)
    assert not [f for f in result.findings if f.finding_class is Finding.BAR_MISSING]


def test_a_non_string_class_key_in_the_overlay_is_a_finding_not_a_crash() -> None:
    doc = overlay()
    wide: dict[object, object] = {}
    wide.update(bars(doc))
    wide[1] = bars(doc)[CLASSES[0]]
    doc["task_class_bars"] = wide
    result = check(overlay_doc=doc)
    assert result.ok is False
    assert ("<overlay>", Finding.OVERLAY_INVALID) in findings(result)


@pytest.mark.parametrize("task_class", CLASSES)
def test_string_class_keys_still_pass_after_non_string_keys_are_judged(
    task_class: str,
) -> None:
    """Control: the shipped documents hold only string keys and stay clean."""
    assert not [f for f in check().findings if f.task_class == task_class]
