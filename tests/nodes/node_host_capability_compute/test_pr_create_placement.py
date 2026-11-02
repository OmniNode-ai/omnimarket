# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Placement of a unit that opens a pull request, from each candidate host's ``scm-pr-create`` probe reading."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import omnimarket.nodes as nodes_package
from omnimarket.nodes.node_host_capability_compute.handlers import (
    handler_host_capability as cap,
)
from omnimarket.nodes.node_host_capability_compute.handlers.handler_pr_create_placement import (
    HandlerPrCreatePlacement,
)
from omnimarket.nodes.node_host_capability_compute.models import (
    EnumPrCreateRefusal,
    ModelHostReading,
    ModelPrCreatePlacementRequest,
)

pytestmark = pytest.mark.unit

# Host names in these tests are fixture labels read from a stand-in table; the handler never sees a literal.
ABSENT_CREDENTIAL = (
    "PARITY-CAP scm-pr-create missing lane-declaration-not-found:config/lab/ci_bus_lanes.yaml\n"
    "PARITY-CAP git-workflow-push ok scopes=repo,workflow\n"
)
READS_OK = "PARITY-CAP scm-pr-create ok subscribed:onex.evt.omnimarket.github-repo-write-completed.v1\n"
RECORDED_GROUP_ERROR = (
    "PARITY-CAP scm-pr-create unknown bus-check-failed:AttributeError:"
    "'ScmBusCaller'_object_has_no_attribute_'_group'\n"
)
NEEDS_PR_CREATE = frozenset(cap.LANE_CAPABILITIES)


def reading(host: str, probe_text: str) -> ModelHostReading:
    return ModelHostReading(
        host=host, capabilities=cap.parse_capability_lines(probe_text)
    )


def place(
    *hosts: ModelHostReading, needs: frozenset[str] = NEEDS_PR_CREATE
) -> dict[str, tuple[bool, EnumPrCreateRefusal | None]]:
    result = HandlerPrCreatePlacement().handle(
        ModelPrCreatePlacementRequest(needs=needs, hosts=hosts)
    )
    return {v.host: (v.admitted, v.refusal) for v in result.verdicts}


def test_pr_create_refused_without_credential() -> None:
    """The reading the lane merge-int-pr-1102 host gave (no ``~/.omnibase/.env``, no declared login) refuses a PR-creating unit."""
    result = HandlerPrCreatePlacement().handle(
        ModelPrCreatePlacementRequest(
            needs=NEEDS_PR_CREATE, hosts=(reading("lab-a", ABSENT_CREDENTIAL),)
        )
    )
    (verdict,) = result.verdicts
    assert (verdict.admitted, verdict.refusal, verdict.reading) == (
        False,
        EnumPrCreateRefusal.CAPABILITY_MISSING,
        "missing",
    )
    assert verdict.reason.startswith("CAPABILITY-MISSING scm-pr-create:")
    assert "lane-declaration-not-found" in verdict.reason
    assert cap.OWNER_FIX[cap.SCM_PR_CREATE] in verdict.reason


def test_pr_create_refused_without_credential_even_when_it_is_the_only_host() -> None:
    assert place(reading("lab-a", ABSENT_CREDENTIAL)) == {
        "lab-a": (False, EnumPrCreateRefusal.CAPABILITY_MISSING)
    }


def test_a_unit_that_needs_no_pr_creation_is_admitted_on_a_missing_host() -> None:
    assert place(
        reading("lab-a", ABSENT_CREDENTIAL), needs=frozenset({cap.SHELLCHECK})
    ) == {"lab-a": (True, None)}


def test_unknown_never_passes_beside_a_host_reading_ok() -> None:
    """The recorded ``_group`` error line is an ``unknown`` reading; beside a host that reads ``ok`` it is refused, not passed."""
    outcome = place(
        reading("lab-a", RECORDED_GROUP_ERROR),
        reading("lab-b", READS_OK),
        reading("lab-c", ""),
    )
    assert outcome == {
        "lab-a": (False, EnumPrCreateRefusal.UNKNOWN_WHILE_PEER_READS_OK),
        "lab-b": (True, None),
        "lab-c": (False, EnumPrCreateRefusal.UNKNOWN_WHILE_PEER_READS_OK),
    }


def test_unknown_never_passes_does_not_turn_missing_into_a_peer_ok_or_refuse_the_ok_host() -> (
    None
):
    outcome = place(
        reading("lab-a", ABSENT_CREDENTIAL),
        reading("lab-b", RECORDED_GROUP_ERROR),
        reading("lab-c", READS_OK),
    )
    assert outcome["lab-a"] == (False, EnumPrCreateRefusal.CAPABILITY_MISSING)
    assert outcome["lab-b"] == (False, EnumPrCreateRefusal.UNKNOWN_WHILE_PEER_READS_OK)
    assert outcome["lab-c"] == (True, None)


def test_unknown_is_admitted_as_before_when_no_host_reads_ok_and_widens_nothing() -> (
    None
):
    """No candidate reads ``ok``: ``unknown`` and an unread host keep the admission they had; ``missing`` is still refused."""
    outcome = place(
        reading("lab-a", RECORDED_GROUP_ERROR),
        reading("lab-b", ""),
        reading("lab-c", ABSENT_CREDENTIAL),
    )
    assert outcome == {
        "lab-a": (True, None),
        "lab-b": (True, None),
        "lab-c": (False, EnumPrCreateRefusal.CAPABILITY_MISSING),
    }
    assert (
        cap.capability_failures(
            reading("lab-a", RECORDED_GROUP_ERROR).capabilities, NEEDS_PR_CREATE
        )
        == []
    )


def test_the_recorded_group_error_parses_as_unknown() -> None:
    (parsed,) = cap.parse_capability_lines(RECORDED_GROUP_ERROR)
    assert (parsed.name, parsed.verdict) == (cap.SCM_PR_CREATE, "unknown")


HOST_NAME = re.compile(r"\bh\d{3}\b|\b\d{1,3}(?:\.\d{1,3}){3}\b")
PLACEMENT_AND_FILL_NODES = (
    "node_host_capability_compute",
    "node_lab_placement_compute",
    "node_lab_fill_effect",
    "node_lab_fill_orchestrator",
    "node_lab_fill_plan_compute",
    "node_lab_fill_receipt_table_compute",
    "node_lab_fill_selection_compute",
    "node_throughput_tick_decision_compute",
    "node_hourly_tick_decision_compute",
    "node_hourly_tick_effect",
    "node_landing_placement_wait_effect",
)


def host_literals(source: str) -> list[str]:
    """String constants of ``source`` that spell a lab host name or address, docstrings left out."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and HOST_NAME.search(node.value)
    ]


def test_host_literal_scan_catches_a_planted_pin() -> None:
    """Positive control: the scan flags a pinned host name and address and passes a docstring and a table read."""
    assert host_literals('PIN = "h' + '201"') == ["h" + "201"]
    assert host_literals('TARGET = "192.168.' + '86.201"') == ["192.168." + "86.201"]
    assert host_literals('"""h' + '201 in prose."""\nrow = table["name"]') == []


def test_pin_not_hardcoded_in_the_placement_and_lab_fill_and_tick_nodes() -> None:
    root = Path(nodes_package.__path__[0])
    sources = [
        p
        for node in PLACEMENT_AND_FILL_NODES
        for p in sorted((root / node).rglob("*.py"))
    ]
    assert len(sources) > 10, sources
    found = {
        str(p.relative_to(root)): lits
        for p in sources
        if (lits := host_literals(p.read_text(encoding="utf-8")))
    }
    assert found == {}
