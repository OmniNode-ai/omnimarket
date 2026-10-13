# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20802: the automation-liveness exposures are read by the status page.

Each of the three bus-backed exposures declares a typed ``backend_readers``
entry, and the status page reads that slot through its contract declaration.
"""

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

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_automation_liveness"
    / "contract.yaml"
)

SLOT_BY_TOPIC = {
    "onex.snapshot.projection.automation-liveness.v1": "automation_liveness",
    "onex.snapshot.projection.automation-liveness-runs.v1": "automation_liveness_runs",
    "onex.snapshot.projection.automation-liveness-alarms.v1": (
        "automation_liveness_alarms"
    ),
}


def _exposures() -> dict[str, ProjectionTableConfig]:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    loaded = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    return {exposure.topic: exposure for exposure in loaded}


def test_every_exposure_declares_its_status_page_slot() -> None:
    exposures = _exposures()
    assert set(exposures) == set(SLOT_BY_TOPIC)
    for topic, slot in SLOT_BY_TOPIC.items():
        readers = exposures[topic].backend_readers
        assert [(r.id, r.kind, r.route, r.projection_slot) for r in readers] == [
            ("onex_status_page", "projection_status_page", "/", slot)
        ]


def test_no_exposure_opts_out_of_reader_coverage() -> None:
    projection = yaml.safe_load(CONTRACT_PATH.read_text())["projection_api"]
    assert "consumers" not in projection
    assert "consumers_reason" not in projection
    for exposure in projection["exposures"]:
        assert "consumers" not in exposure
        assert "consumers_reason" not in exposure


def test_status_page_reads_each_slot_from_its_declared_exposure() -> None:
    exposures = _exposures()
    now = datetime(2026, 10, 13, 9, tzinfo=UTC)
    rows = {
        "onex.snapshot.projection.automation-liveness.v1": [
            {"process_key": "p1", "verdict": "ALIVE"}
        ],
        "onex.snapshot.projection.automation-liveness-runs.v1": [
            {"run_key": "r1", "outcome": "ok"}
        ],
        "onex.snapshot.projection.automation-liveness-alarms.v1": [
            {"episode_id": "<b>e1</b>", "state": "OPEN"}
        ],
    }
    view = TablePageView(rows=rows, latest=dict.fromkeys(rows, now))
    page = build_morning_page(exposures, view, service_name="test")

    for topic, slot in SLOT_BY_TOPIC.items():
        read = getattr(page, slot)
        assert read.topic == topic
        assert read.state == EnumPanelState.LIVE
        assert list(read.rows) == rows[topic]

    document = render_morning_page(page)
    assert "automation liveness alarms" in document
    assert "&lt;b&gt;e1&lt;/b&gt;" in document
    assert "<b>e1</b>" not in document


def test_status_page_refuses_a_slot_with_no_declaring_exposure() -> None:
    page = build_morning_page({}, TablePageView(), service_name="test")
    for slot in SLOT_BY_TOPIC.values():
        read = getattr(page, slot)
        assert read.state == EnumPanelState.REFUSED
        assert read.reason_code == "backend_reader_missing"
