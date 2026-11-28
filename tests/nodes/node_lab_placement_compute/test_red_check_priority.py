# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26624 -- a lane on a red delegation check takes the class the contract declares for it.

The contract's ``priority.red_checks`` block says which delegation checks count, the class a red one takes and
the ordered sources its result is read from; the handler holds no check id and no class.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_placement_compute.handlers import (
    HandlerLabLanePriority,
    load_priority_rules,
    read_red_checks,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    ModelLanePriorityRequest,
    ModelRedCheckReading,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lab_placement_compute"
)
FIXTURE = Path(__file__).parent / "fixtures" / "red_check_1123" / "claims.json"


def _lanes() -> list[dict[str, str]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["lanes"])


# --- AC1: the contract declares the input as data ------------------------------------------------------------------
def test_contract_declares_the_red_check_input() -> None:
    rules = load_priority_rules()
    red = rules.red_checks
    assert red is not None
    assert red.class_ in rules.classes
    assert red.class_ == rules.classes[0]
    assert {"C13", "C14", "C29"} <= set(red.checks)
    assert red.sources == ("regression-event", "probe-results")


def _code_strings(path: Path) -> list[str]:
    """Every string constant of the module outside its docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
    ]


def test_handler_holds_no_check_id_and_no_declared_class_in_code() -> None:
    strings = _code_strings(NODE_DIR / "handlers" / "handler_lab_lane_priority.py")
    assert strings  # the walk reads the module
    assert not [s for s in strings if re.search(r"\bC[0-9]{2}\b", s)]
    assert not [s for s in strings if s in load_priority_rules().classes]


def test_priority_class_args_passes_the_red_check_class() -> None:
    from omnimarket.nodes.node_lab_placement_compute.handlers import priority_class_args

    red = ModelRedCheckReading(red=("C13",), read_from="probe-results")
    assert priority_class_args("C13 times out", red_check_reading=red) == [
        "--priority-class",
        "factory-unblock",
    ]
    assert priority_class_args("C13 times out") == []
    assert (
        priority_class_args("Class: feature\nC13 times out", red_check_reading=red)[1]
        == "factory-unblock"
    )


# --- AC2: red resolves to the declared class, green as today -------------------------------------------------------
def test_red_check_in_ticket_text_takes_the_declared_class() -> None:
    handler = HandlerLabLanePriority()
    declared = handler.rules.red_checks
    assert declared is not None
    request = ModelLanePriorityRequest(
        repo="omnimarket",
        ticket="OMN-26534",
        ticket_text="the customer-local check (C13) times out",
        red_check_reading=ModelRedCheckReading(red=("C13",), read_from="probe-results"),
    )
    result = handler.handle(request)
    assert (result.priority_class, result.source) == (declared.class_, "red-check")
    assert result.red_checks == ("C13",)


def test_same_lane_with_the_check_green_resolves_as_today() -> None:
    handler = HandlerLabLanePriority()
    base = {
        "repo": "omnimarket",
        "ticket": "OMN-26534",
        "ticket_text": "the customer-local check (C13) times out",
    }
    green = handler.handle(
        ModelLanePriorityRequest(
            **base,
            red_check_reading=ModelRedCheckReading(red=(), read_from="probe-results"),
        )
    )
    today = handler.handle(ModelLanePriorityRequest(**base))
    assert (green.priority_class, green.source) == ("feature", "repository")
    assert (green.priority_class, green.source) == (today.priority_class, today.source)
    assert green.red_check_report is None


def test_a_red_check_the_lane_does_not_name_changes_nothing() -> None:
    result = HandlerLabLanePriority().handle(
        ModelLanePriorityRequest(
            repo="omnimarket",
            ticket_text="a feature",
            red_check_reading=ModelRedCheckReading(
                red=("C14",), read_from="probe-results"
            ),
        )
    )
    assert (result.priority_class, result.source) == ("feature", "repository")


def test_a_red_check_never_demotes_a_lane() -> None:
    handler = HandlerLabLanePriority()
    result = handler.handle(
        ModelLanePriorityRequest(
            repo="omnimarket",
            ticket_text="Class: factory-unblock\nC13 is red",
            red_check_reading=ModelRedCheckReading(
                red=("C13",), read_from="probe-results"
            ),
        )
    )
    assert result.priority_class == "factory-unblock"


def test_the_brief_names_the_check_too() -> None:
    result = HandlerLabLanePriority().handle(
        ModelLanePriorityRequest(
            repo="omnimarket",
            brief_text="Lane brief: repair C29, the own-key clause",
            red_check_reading=ModelRedCheckReading(
                red=("C29",), read_from="regression-event"
            ),
        )
    )
    assert (result.priority_class, result.source) == ("factory-unblock", "red-check")


def test_a_check_id_inside_a_longer_word_is_not_named() -> None:
    result = HandlerLabLanePriority().handle(
        ModelLanePriorityRequest(
            repo="omnimarket",
            ticket_text="the c13red lane and the C130 table",
            red_check_reading=ModelRedCheckReading(
                red=("C13",), read_from="probe-results"
            ),
        )
    )
    assert result.source == "repository"


# --- AC4: the recorded 2026-11-23 lanes ran as other and now take the declared class --------------------------------
def test_red_check_recorded_lanes_were_other_and_now_take_the_declared_class() -> None:
    handler = HandlerLabLanePriority()
    lanes = _lanes()
    assert {lane["check"] for lane in lanes} == {"C13", "C14", "C29"}
    for lane in lanes:
        before = handler.handle(
            ModelLanePriorityRequest(
                repo="other_repo",
                ticket=lane["ticket"],
                ticket_text=lane["ticket_text"],
            )
        )
        assert before.priority_class == "other"  # what the CLAIM rows recorded
        after = handler.handle(
            ModelLanePriorityRequest(
                repo="other_repo",
                ticket=lane["ticket"],
                ticket_text=lane["ticket_text"],
                red_check_reading=ModelRedCheckReading(
                    red=(lane["check"],), read_from="probe-results"
                ),
            )
        )
        assert (after.priority_class, after.source) == ("factory-unblock", "red-check")


# --- AC5: an unreadable result is never green ------------------------------------------------------------------------
def test_unreadable_result_is_reported_and_the_lane_falls_back_as_today() -> None:
    handler = HandlerLabLanePriority()
    reading = ModelRedCheckReading(
        unreadable="regression-event: no reader; probe-results: refused"
    )
    result = handler.handle(
        ModelLanePriorityRequest(
            repo="omnimarket",
            ticket_text="the customer-local check (C13) times out",
            red_check_reading=reading,
        )
    )
    assert (result.priority_class, result.source) == ("feature", "repository")
    assert result.red_check_report is not None
    assert result.red_check_report.startswith("RED-CHECK-UNREADABLE:")
    assert "C13" in result.red_check_report
    assert "regression-event: no reader" in result.red_check_report


def test_no_reading_at_all_says_so_for_a_lane_naming_a_check() -> None:
    result = HandlerLabLanePriority().handle(
        ModelLanePriorityRequest(repo="omnimarket", ticket_text="C14 is red")
    )
    assert result.red_check_report is not None
    assert result.red_check_report.startswith("RED-CHECK-UNREAD:")
    quiet = HandlerLabLanePriority().handle(
        ModelLanePriorityRequest(repo="omnimarket", ticket_text="a feature")
    )
    assert quiet.red_check_report is None


def test_reader_falls_through_the_sources_in_the_declared_order() -> None:
    rules = load_priority_rules()
    calls: list[str] = []

    def broken() -> list[str]:
        calls.append("regression-event")
        raise OSError("event store refused")

    def probes() -> list[str]:
        calls.append("probe-results")
        return ["c14", "X99"]

    reading = read_red_checks(
        rules, {"regression-event": broken, "probe-results": probes}
    )
    assert calls == ["regression-event", "probe-results"]
    assert reading.red == ("C14",)  # an id the contract does not declare is not carried
    assert reading.read_from == "probe-results"
    assert reading.unreadable is None


def test_reader_with_every_source_unreadable_is_unreadable_not_green() -> None:
    rules = load_priority_rules()

    def broken() -> list[str]:
        raise OSError("event store refused")

    reading = read_red_checks(rules, {"regression-event": broken})
    assert reading.red == ()
    assert reading.unreadable is not None
    assert "regression-event: OSError: event store refused" in reading.unreadable
    assert "probe-results: no reader" in reading.unreadable
