# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22122 -- lane priority classes: factory-unblock, factory-fix, feature, other.

The classes, their order and their holdbacks are the node contract's data; a ticket carries its class in a
``Class:`` line; factory machinery is the epics of the private overlay.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV, NodeOverlayError
from omnimarket.nodes.node_lab_placement_compute.handlers import (
    HandlerLabLanePriority,
    class_line,
    load_factory_epics,
    load_priority_rules,
    priority_class_args,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    ModelLanePriorityRequest,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lab_placement_compute"
)
CLASSES = ("factory-unblock", "factory-fix", "feature", "other")


@pytest.fixture(scope="module")
def priority() -> HandlerLabLanePriority:
    return HandlerLabLanePriority()


def _overlay_env(tmp_path: Path, document: object) -> dict[str, str]:
    path = tmp_path / "lane_priority" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return {OVERLAY_ROOTS_ENV: str(tmp_path)}


# --- AC1: the contract file holds the classes, their order and their holdbacks -----------------------------------
def test_contract_lists_the_classes_in_order_with_a_holdback_each() -> None:
    rules = load_priority_rules()
    assert rules.classes == CLASSES
    assert rules.holdback == {
        "factory-unblock": 0,
        "factory-fix": 1,
        "feature": 2,
        "other": 3,
    }


def test_contract_has_no_occ_retirement_anywhere() -> None:
    text = (NODE_DIR / "contract.yaml").read_text()
    priority_block = yaml.safe_load(text)["priority"]
    assert "occ-retirement" not in yaml.safe_dump(priority_block)
    assert "occ-retirement" not in load_priority_rules().classes


def test_no_class_name_is_a_literal_in_the_node_code() -> None:
    # positive control: the contract does name them, so the scan below is looking for something real
    assert all(name in (NODE_DIR / "contract.yaml").read_text() for name in CLASSES)
    quoted = re.compile(
        r"""["'](?:factory-unblock|factory-fix|feature|occ-retirement|mvp-landing|process-fix)["']"""
    )
    offenders = [
        str(path.relative_to(NODE_DIR))
        for path in NODE_DIR.rglob("*.py")
        if quoted.search(path.read_text())
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "document",
    [
        {"classes": ["a", "other"], "holdback": {"a": 0}, "factory_class": "a"},
        {
            "classes": ["a", "other"],
            "holdback": {"a": 0, "other": -1},
            "factory_class": "a",
        },
        {
            "classes": ["a", "other"],
            "holdback": {"a": 0, "other": 1},
            "factory_class": "x",
        },
        {"classes": ["a", "b"], "holdback": {"a": 0, "b": 1}, "factory_class": "a"},
    ],
)
def test_contract_loader_refuses_a_malformed_priority_block(
    document: dict[str, object],
) -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_lab_placement_compute.models import ModelPriorityRules

    with pytest.raises(ValidationError):
        ModelPriorityRules.model_validate(document)


# --- the Class line ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Title: x\nClass: factory-fix\nGate: y", "factory-fix"),
        ("> Class: Factory-Unblock", "factory-unblock"),
        ("- Class: `feature`", "feature"),
        ("Title: x\nno class here", None),
        ("The Class: of work is mentioned mid-line, Class: other", None),
        ("", None),
        (None, None),
    ],
)
def test_class_line_reads_the_first_line_that_is_one(
    text: str | None, expected: str | None
) -> None:
    assert class_line(text) == expected


def test_class_line_outranks_the_repository_and_the_declared_class_outranks_the_line(
    priority: HandlerLabLanePriority,
) -> None:
    text = "Title: x\nClass: factory-fix\n"
    line = priority.handle(
        ModelLanePriorityRequest(repo="omnibase_infra", ticket_text=text)
    )
    assert (line.priority_class, line.source, line.fallback) == (
        "factory-fix",
        "class-line",
        None,
    )
    flag = priority.handle(
        ModelLanePriorityRequest(
            repo="omnibase_infra", ticket_text=text, declared="factory-unblock"
        )
    )
    assert (flag.priority_class, flag.source) == ("factory-unblock", "declared")


def test_ticket_text_without_a_class_line_falls_back_to_the_repository_and_reports_it(
    priority: HandlerLabLanePriority,
) -> None:
    result = priority.handle(
        ModelLanePriorityRequest(
            repo="omnibase_infra", ticket_text="Title: x\nNo line.\n"
        )
    )
    assert (result.priority_class, result.source) == ("feature", "repository")
    assert result.fallback is not None
    assert result.fallback.startswith("CLASS-LINE-MISSING")
    assert "feature from repository" in result.fallback


def test_a_class_line_naming_no_class_is_reported_and_falls_back(
    priority: HandlerLabLanePriority,
) -> None:
    result = priority.handle(
        ModelLanePriorityRequest(repo="some_other_repo", ticket_text="Class: urgent")
    )
    assert (result.priority_class, result.source) == ("other", "default")
    assert result.fallback is not None
    assert result.fallback.startswith("CLASS-LINE-UNKNOWN")


def test_no_ticket_text_read_reports_no_fallback(
    priority: HandlerLabLanePriority,
) -> None:
    # negative control: the caller read no ticket, so a missing line is not the fallback to report
    result = priority.handle(ModelLanePriorityRequest(repo="omnibase_infra"))
    assert result.fallback is None


# --- AC4: factory machinery is the overlay's epics ---------------------------------------------------------------
def test_ticket_whose_parent_is_a_listed_factory_epic_resolves_to_the_factory_class(
    priority: HandlerLabLanePriority, tmp_path: Path
) -> None:
    epics = load_factory_epics(_overlay_env(tmp_path, {"factory_epics": ["OMN-1001"]}))
    assert epics == ("OMN-1001",)
    listed = priority.handle(
        ModelLanePriorityRequest(
            repo="omnimarket", ticket="OMN-2", parent="omn-1001", factory_epics=epics
        )
    )
    assert (listed.priority_class, listed.source) == ("factory-fix", "factory-epic")
    unlisted = priority.handle(
        ModelLanePriorityRequest(
            repo="omnimarket", ticket="OMN-2", parent="OMN-1002", factory_epics=epics
        )
    )
    assert (unlisted.priority_class, unlisted.source) == ("feature", "repository")


def test_a_class_line_outranks_the_factory_epic_default(
    priority: HandlerLabLanePriority,
) -> None:
    result = priority.handle(
        ModelLanePriorityRequest(
            repo="omnimarket",
            parent="OMN-1001",
            factory_epics=("OMN-1001",),
            ticket_text="Class: feature",
        )
    )
    assert (result.priority_class, result.source) == ("feature", "class-line")


def test_no_overlay_lists_no_factory_epics_and_a_broken_one_is_refused(
    tmp_path: Path,
) -> None:
    assert load_factory_epics({OVERLAY_ROOTS_ENV: str(tmp_path)}) == ()
    with pytest.raises(NodeOverlayError, match="fields"):
        load_factory_epics(_overlay_env(tmp_path, {"factory_epics": []}))


# --- AC5: the argument a dispatcher builds from a ticket ---------------------------------------------------------
def test_priority_class_args_pass_the_class_a_ticket_names() -> None:
    assert priority_class_args("Title: x\nClass: factory-fix\n") == [
        "--priority-class",
        "factory-fix",
    ]
    assert priority_class_args("Title: x\n") == []
    assert priority_class_args("Class: mvp-landing") == []
    assert priority_class_args(None) == []


# --- AC7: slot admission with one free slot ----------------------------------------------------------------------
def test_one_free_slot_goes_to_factory_fix_before_feature(
    priority: HandlerLabLanePriority,
) -> None:
    waiting = ("factory-fix", "feature")
    fix = priority.handle(
        ModelLanePriorityRequest(declared="factory-fix", free_slots=1, waiting=waiting)
    )
    feat = priority.handle(
        ModelLanePriorityRequest(declared="feature", free_slots=1, waiting=waiting)
    )
    assert fix.admitted
    assert not feat.admitted
    assert feat.refusal is not None
    assert feat.refusal.startswith("PRIORITY-HOLDBACK")


def test_the_last_slot_goes_to_factory_unblock_before_feature_and_before_factory_fix(
    priority: HandlerLabLanePriority,
) -> None:
    waiting = ("factory-unblock", "factory-fix", "feature")
    admitted = {
        cls: priority.handle(
            ModelLanePriorityRequest(declared=cls, free_slots=1, waiting=waiting)
        ).admitted
        for cls in CLASSES
    }
    assert admitted == {
        "factory-unblock": True,
        "factory-fix": False,
        "feature": False,
        "other": False,
    }


def test_a_class_holds_back_no_more_than_its_contract_holdback(
    priority: HandlerLabLanePriority,
) -> None:
    # factory-fix holds back 1: with two free slots it takes one even while factory-unblock waits
    result = priority.handle(
        ModelLanePriorityRequest(
            declared="factory-fix", free_slots=2, waiting=("factory-unblock",)
        )
    )
    assert result.admitted
    # feature holds back 2: with two free slots and two classes above waiting, it is still held
    held = priority.handle(
        ModelLanePriorityRequest(
            declared="feature", free_slots=2, waiting=("factory-unblock", "factory-fix")
        )
    )
    assert not held.admitted
