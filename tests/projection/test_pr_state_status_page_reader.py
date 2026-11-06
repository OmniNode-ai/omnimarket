# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22542: the status page reads the pr-state exposure.

node_projection_pr_state declares one typed ``backend_readers`` entry, and the
status page reads that slot through its contract declaration. The
exposure-reader-coverage gate resolves the same triple (reader id, route,
slot) against the ``read_backend_projection`` calls this page makes.
"""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.morning_page import (
    EnumPanelState,
    build_morning_page,
    render_morning_page,
)
from omnimarket.projection.table_reader import TablePageView

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = (
    ROOT / "src" / "omnimarket" / "nodes" / "node_projection_pr_state" / "contract.yaml"
)
MORNING_PAGE = ROOT / "src" / "omnimarket" / "projection" / "morning_page.py"
TOPIC = "onex.snapshot.projection.pr-state.v1"
DECLARED = ("onex_status_page", "projection_status_page", "/", "pr_state")


def _exposures(contract: dict[str, object]) -> dict[str, ProjectionTableConfig]:
    loaded = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    return {exposure.topic: exposure for exposure in loaded}


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


def _page_slots() -> set[tuple[str, str, str]]:
    """The (reader id, route, slot) triples the page reads, lifted from its calls."""
    found: set[tuple[str, str, str]] = set()
    for node in ast.walk(ast.parse(MORNING_PAGE.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "read_backend_projection"
        ):
            kwargs = {
                kw.arg: kw.value.value
                for kw in node.keywords
                if isinstance(kw.value, ast.Constant)
            }
            if {"reader_id", "route", "projection_slot"} <= kwargs.keys():
                found.add(
                    (
                        str(kwargs["reader_id"]),
                        str(kwargs["route"]),
                        str(kwargs["projection_slot"]),
                    )
                )
    return found


def test_backend_reader_declaration_matches_a_status_page_read() -> None:
    exposures = _exposures(_contract())
    readers = exposures[TOPIC].backend_readers
    assert [(r.id, r.kind, r.route, r.projection_slot) for r in readers] == [DECLARED]
    triple = (DECLARED[0], DECLARED[2], DECLARED[3])
    assert triple in _page_slots()
    # The same comparison refuses a declaration the page does not read.
    assert (DECLARED[0], DECLARED[2], "pr_states") not in _page_slots()


def test_exposure_does_not_opt_out_of_reader_coverage() -> None:
    projection = _contract()["projection_api"]
    assert "consumers" not in projection
    assert "consumers_reason" not in projection


def test_status_page_reads_the_slot_from_its_declared_exposure() -> None:
    now = datetime(2026, 11, 6, 2, tzinfo=UTC)
    rows = [
        {"repo": "omnimarket", "pr_number": 1, "state": "open", "ci_verdict": "RED"},
        {"repo": "<b>x</b>", "pr_number": 2, "state": "merged", "ci_verdict": "GREEN"},
    ]
    view = TablePageView(rows={TOPIC: rows}, latest={TOPIC: now})
    page = build_morning_page(_exposures(_contract()), view, service_name="test")

    assert page.pr_state.topic == TOPIC
    assert page.pr_state.state == EnumPanelState.LIVE
    assert list(page.pr_state.rows) == rows

    document = render_morning_page(page)
    assert "pull request state" in document
    assert "&lt;b&gt;x&lt;/b&gt;" in document
    assert "<b>x</b>" not in document


def test_render_names_the_panel_when_the_exposure_is_absent() -> None:
    page = build_morning_page({}, TablePageView(), service_name="test")
    assert page.pr_state.state == EnumPanelState.REFUSED
    assert page.pr_state.reason_code == "backend_reader_missing"
    document = render_morning_page(page)
    assert "pull request state" in document
    assert "REFUSED: backend_reader_missing" in document


def test_status_page_refuses_an_exposure_declaring_another_slot() -> None:
    contract = _contract()
    entries = contract["projection_api"]["backend_readers"] if False else None
    assert entries is None
    projection = contract["projection_api"]
    projection["backend_readers"] = [
        {
            "id": "onex_status_page",
            "kind": "projection_status_page",
            "route": "/",
            "projection_slot": "pr_states",
        }
    ]
    page = build_morning_page(_exposures(contract), TablePageView(), service_name="t")
    assert page.pr_state.state == EnumPanelState.REFUSED
    assert page.pr_state.reason_code == "backend_reader_missing"
