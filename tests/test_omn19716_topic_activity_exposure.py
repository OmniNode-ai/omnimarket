# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19716 projection exposure contract tests."""

from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig

pytestmark = pytest.mark.unit
NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_topic_activity"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _exposure() -> ProjectionTableConfig:
    contract = _contract()
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1
    return exposures[0]


def test_exposure_is_bus_backed_with_cursor_and_freshness() -> None:
    exposure = _exposure()
    assert exposure.topic == "onex.snapshot.projection.topic-activity.v1"
    assert exposure.bus_backed is True
    assert exposure.cursor_column == "projection_cursor"
    assert exposure.freshness_column == "sampled_at"
    assert exposure.expected_event_interval_seconds == 60
    assert exposure.key_columns == ("topic",)


def test_most_active_topics_are_ordered_first() -> None:
    exposure = _exposure()
    assert exposure.order_by_spec == (
        ("rate_last_hour_per_second", "DESC", "LAST"),
        ("topic", "ASC", None),
    )


def test_all_exposed_columns_exist_in_the_owned_migration() -> None:
    sql = (NODE_DIR / "migrations" / "0000_create_topic_activity.sql").read_text()
    for column in _exposure().columns:
        assert column in sql


def test_exposure_does_not_opt_out_of_reader_coverage() -> None:
    projection = _contract()["projection_api"]
    assert "consumers" not in projection  # type: ignore[operator]
    assert "consumers_reason" not in projection  # type: ignore[operator]


def test_runtime_dispatch_resolves_only_the_writer() -> None:
    """OMN-19721 found a routed pure fold dead-lettering every event beside its
    writer; this reducer routes the writer alone."""
    from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract

    contract = _parse_contract(
        contract_path=CONTRACT_PATH,
        entry_point_name="node_projection_topic_activity",
        package_name="omnimarket",
        package_version="test",
    )
    assert contract.handler_routing is not None
    assert [entry.handler.name for entry in contract.handler_routing.handlers] == [
        "TopicActivityProjectionWriter"
    ]


def test_health_lists_topic_activity_as_bus_backed_without_connecting() -> None:
    from fastapi.testclient import TestClient

    from omnimarket.projection.api_server import app, get_row_source, get_topic_map
    from omnimarket.projection.table_reader import TableRowSource

    exposure = _exposure()
    unbacked = exposure.model_copy(update={"topic": "unbacked", "bus_backed": False})
    app.dependency_overrides[get_topic_map] = lambda: {
        exposure.topic: exposure,
        unbacked.topic: unbacked,
    }
    app.dependency_overrides[get_row_source] = lambda: TableRowSource(environ={})
    try:
        response = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["bus_backed_topics"] == [exposure.topic]
    assert response.json()["served_topics"] == [exposure.topic]
    assert response.json()["backing"] == "table"


class _TopicPanelRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_panel = False
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "section" and dict(attrs).get("id") == "topic-activity":
            self._in_panel = True
        if self._in_panel and tag == "tr":
            self.rows.append([])
        if self._in_panel and tag == "td":
            self._in_cell = True
            self.rows[-1].append("")

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self.rows[-1][-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._in_cell = False
        if tag == "section":
            self._in_panel = False


@pytest.mark.parametrize("state", ["ACTIVE", "QUIET", "UNKNOWN"])
def test_status_route_renders_topic_counts_and_unknown(state: str) -> None:
    from fastapi.testclient import TestClient

    from omnimarket.events.topic_activity import ModelTopicActivitySample
    from omnimarket.nodes.node_projection_topic_activity.handlers.handler_projection_topic_activity import (
        HandlerProjectionTopicActivity,
    )
    from omnimarket.nodes.node_projection_topic_activity.models import (
        ModelTopicActivityProjectionRequest,
    )
    from omnimarket.projection.api_server import app, get_row_source, get_topic_map
    from omnimarket.projection.table_reader import TablePageView, TableRowSource

    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    topic = "onex.evt.omniclaude.tool-executed.v1"
    # Independent offset-window reference: sum(high - offsets_for_times).
    reference = (240 - 100) + (170 - 110) if state == "ACTIVE" else 0
    sample = ModelTopicActivitySample(
        topic=topic,
        high_watermark_total=410,
        low_watermark_total=0,
        messages_last_hour=reference,
        messages_last_24h=300,
        newest_message_at=now,
        retention_truncated=False,
    )
    row = (
        HandlerProjectionTopicActivity()
        .handle(
            ModelTopicActivityProjectionRequest(
                topic=topic,
                sample=sample if state != "UNKNOWN" else None,
                sampled_at=now if state != "UNKNOWN" else None,
            )
        )
        .row.model_dump(mode="json")
    )
    exposure = _exposure()
    view = TablePageView(rows={exposure.topic: [row]}, latest={exposure.topic: now})

    class _Source(TableRowSource):
        async def page_view(
            self, topic_map: dict[str, ProjectionTableConfig], *, tenant_id: str | None
        ) -> TablePageView:
            return view

    app.dependency_overrides[get_topic_map] = lambda: {exposure.topic: exposure}
    app.dependency_overrides[get_row_source] = lambda: _Source(environ={})
    try:
        response = TestClient(app).get("/")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    parser = _TopicPanelRows()
    parser.feed(response.text)
    rendered = next(cells for cells in parser.rows if cells and cells[0] == topic)
    if state != "UNKNOWN":
        assert int(rendered[2]) == reference
        assert rendered[1] == state
        assert rendered[4] == "UNKNOWN"  # first sample has no delta-rate history
        assert rendered[6] == "0.0"  # measured newest-event age can be zero
    else:
        assert rendered[1:] == ["UNKNOWN"] * 8


def test_topic_panel_preserves_database_refusal_and_escapes_names() -> None:
    from omnimarket.projection.morning_page import (
        build_morning_page,
        render_morning_page,
    )
    from omnimarket.projection.table_reader import TablePageView

    exposure = _exposure()
    view = TablePageView(
        failures={exposure.topic: ("projection_database_unavailable", "unreachable")}
    )
    document = render_morning_page(
        build_morning_page({exposure.topic: exposure}, view, service_name="test")
    )
    panel = document.split('<section id="topic-activity">')[1].split("</section>")[0]
    assert "projection_database_unavailable" in panel
    assert "<td>0</td>" not in panel
    row: dict[str, Any] = {
        "topic": "<script>alert(1)</script>",
        "activity_state": "UNKNOWN",
    }
    view = TablePageView(rows={exposure.topic: [row]})
    document = render_morning_page(
        build_morning_page({exposure.topic: exposure}, view, service_name="test")
    )
    assert row["topic"] not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document


@pytest.mark.parametrize("total", [9, 2025])
async def test_topic_panel_reads_every_cursor_window(total: int) -> None:
    from omnimarket.projection.morning_page import build_morning_page
    from omnimarket.projection.table_reader import TableRowSource

    exposure = _exposure().model_copy(update={"limit": 1})
    rows = [
        {
            "projection_cursor": index,
            "topic": f"topic-{index}",
            "rate_last_hour_per_second": index,
        }
        for index in range(1, total + 1)
    ]

    class _Source(TableRowSource):
        async def rows(
            self,
            cfg: ProjectionTableConfig,
            *,
            since: str | None = None,
            selection: str = "newest",
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            if selection == "newest":
                return rows[-4:]
            return [row for row in rows if row["projection_cursor"] > int(since or 0)][
                :4
            ]

        async def latest_event_at(
            self, cfg: ProjectionTableConfig, **kwargs: Any
        ) -> None:
            return None

    view = await _Source(environ={}).page_view(
        {exposure.topic: exposure}, tenant_id=None
    )
    page = build_morning_page({exposure.topic: exposure}, view, service_name="test")
    assert len(page.topic_activity.rows) == total
    assert [row["topic"] for row in page.topic_activity.rows] == [
        f"topic-{index}" for index in range(total, 0, -1)
    ]


@pytest.mark.parametrize(
    "invalid", [{"cursor_column": None}, {"key_grain": "immutable"}]
)
def test_complete_inventory_reader_requires_a_mutable_cursor(
    invalid: dict[str, Any],
) -> None:
    from pydantic import ValidationError

    fields = _exposure().model_dump()
    fields.update(invalid)
    with pytest.raises(ValidationError, match="read_all_rows requires"):
        ProjectionTableConfig.model_validate(fields)
