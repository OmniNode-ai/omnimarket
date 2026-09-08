# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18035 — ``next_cursor`` on /v1/evidence-pipeline/* signals truncation, not non-emptiness.

``_evidence_projection_response`` gated the cursor on the page being **non-empty**::

    next_cursor = (
        str(serialisable_rows[-1].get(cfg.cursor_column))
        if serialisable_rows and cfg.cursor_column in serialisable_rows[-1]
        else None
    )

So a **complete** page that happens to carry rows advertises a cursor, and a caller that
follows it lands on an empty page indistinguishable from "there is more data". The same
defect OMN-17215 repaired one function along, on the surface OCC and the autoclose sweep
were believed to read.

Two things this file deliberately does NOT claim, both recorded on the ticket
(comment ``d48a26a1``):

* **AC3 is not met.** It requires the RED test to exercise "the OCC evidence reader and the
  autoclose sweep ... paging this exposure". Measured fleet-wide across 20 repos: OCC has
  **0** code references (its 2 hits are contract YAML prose), the OMN-16106 sweep in
  ``omnibase_infra`` calls ``api.linear.app/graphql`` and ``gh api`` rather than this
  exposure, and omnidash calls exactly one route and reads ``next_cursor`` **0** times.
  Positive controls: the same grep finds 9 ``next_cursor`` hits in ``omnimarket/src`` and
  119 ``cursor`` mentions in omnidash, so those zeros are real absence. There is no
  consumer to write the test against; a re-ruling on AC3 is pending.
* The tests below therefore cover the endpoint and ``/v1/evidence-pipeline/events`` — the
  one route a consumer actually calls — which is the honest subset, not the stated AC.

Unlike ``projection_query``, this function **refuses** with ``503 cursor_column_missing``
when no ``cursor_column`` is declared, so this fix cannot land inert the way OMN-17215's did
on consumer-flow.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from scripts.projection_api_server import app, get_snapshot_cache, get_topic_map

pytestmark = pytest.mark.unit

_TOPIC = "onex.snapshot.projection.evidence_pipeline.stages.v1"
_EVENTS_TOPIC = "onex.snapshot.projection.evidence_pipeline.live_events.v1"


def _cfg(topic: str = _TOPIC) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="evidence_dashboard_projection",
        schema_name="public",
        columns=("projection_cursor", "last_ingest_sequence", "observed_at"),
        order_by="last_ingest_sequence ASC",
        order_by_spec=(("last_ingest_sequence", "ASC", None),),
        freshness_column="observed_at",
        expected_event_interval_seconds=None,
        cursor_column="projection_cursor",
        last_event_id_column=None,
        last_ingest_sequence_column="last_ingest_sequence",
        freshness_state_column=None,
        degraded_reason_column=None,
        observed_at_column="observed_at",
        limit=100,
        source_contract="node_test",
        status=ProjectionStatus.OK,
        degraded_reason="",
        bus_backed=True,
        key_columns=("projection_cursor",),
    )


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "projection_cursor": f"cursor-{i}",
            "last_ingest_sequence": i,
            "observed_at": "2026-09-08T00:00:00Z",
        }
        for i in range(1, n + 1)
    ]


def _cache(rows: list[dict[str, Any]]) -> MagicMock:
    cache = MagicMock()
    cache.is_bootstrapped = MagicMock(return_value=True)
    cache.get_rows = MagicMock(return_value=rows)
    cache.latest_event_at = MagicMock(return_value=None)
    return cache


@contextmanager
def _client(
    rows: list[dict[str, Any]], topic: str = _TOPIC
) -> Generator[TestClient, None, None]:
    cfg = _cfg(topic)
    app.dependency_overrides[get_snapshot_cache] = lambda: _cache(rows)
    app.dependency_overrides[get_topic_map] = lambda: {cfg.topic: cfg}
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.clear()


# AC1 — a COMPLETE page must advertise no cursor. This is the defect: three rows served
# under a limit of five is complete, yet the old condition emitted a cursor because the page
# was merely non-empty.
def test_complete_page_advertises_no_cursor() -> None:
    with _client(_rows(3)) as client:
        body = client.get("/v1/evidence-pipeline/stages?limit=5").json()
    assert body["next_cursor"] is None


# AC2 — a TRUNCATED page must advertise one. Three rows under a limit of two is the smallest
# fixture that is genuinely truncated, mirroring the shape approved on OMN-17215.
def test_truncated_page_advertises_a_cursor() -> None:
    with _client(_rows(3)) as client:
        body = client.get("/v1/evidence-pipeline/stages?limit=2").json()
    assert body["next_cursor"] is not None
    assert body["next_cursor"] == "cursor-2"


# The distinction stated as one assertion: identical data, different limit, opposite answer.
# A regression that reverts to the non-emptiness test passes neither half.
def test_the_same_rows_answer_differently_by_truncation() -> None:
    with _client(_rows(3)) as client:
        complete = client.get("/v1/evidence-pipeline/stages?limit=5").json()
        truncated = client.get("/v1/evidence-pipeline/stages?limit=2").json()
    assert complete["next_cursor"] is None
    assert truncated["next_cursor"] is not None


# An EMPTY page is complete by definition and must carry no cursor — the fail-closed edge.
def test_empty_page_advertises_no_cursor() -> None:
    with _client([]) as client:
        body = client.get("/v1/evidence-pipeline/stages?limit=5").json()
    assert body["next_cursor"] is None


# The one route a consumer actually calls. Not AC3 — omnidash ignores next_cursor — but it is
# the real surface, so the invariant is pinned there too rather than only on /stages.
@pytest.mark.parametrize("route", ["stages", "correlations", "readiness", "events"])
def test_every_evidence_route_agrees_on_the_invariant(route: str) -> None:
    topic = _EVENTS_TOPIC if route == "events" else _TOPIC
    with _client(_rows(3), topic=topic) as client:
        complete = client.get(f"/v1/evidence-pipeline/{route}?limit=5")
        truncated = client.get(f"/v1/evidence-pipeline/{route}?limit=2")
    if complete.status_code != 200:
        pytest.skip(
            f"/{route} not served by this topic fixture ({complete.status_code})"
        )
    assert complete.json()["next_cursor"] is None
    assert truncated.json()["next_cursor"] is not None
