# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16290: request-time ``order_by`` must reorder the ACTUAL served rows.

Reported by Daniyal during the OMN-15800 AC re-verification (2026-08-17,
comments 42d572e2 / 3d08047a): ``GET /projection/<topic>`` accepted an
``order_by`` query parameter (FastAPI silently accepts any unrecognised query
param) but always served the fixed contract-declared ``updated_at DESC``
ordering regardless of the requested value -- the parameter reached nothing.

Root cause (2026-08-20 audit lane): the pre-fix endpoint declared only an
``order`` query param (an ``asc``/``desc`` direction flip on the FIRST
contract-declared sort column, wired via ``_effective_order_by_spec`` /
``SnapshotCache.get_rows(order_by_override=...)``). There was no ``order_by``
parameter at all, so it was accepted by FastAPI (no validation error) and had
zero effect on the read path -- exactly what "accepted but silently ignored"
describes. AC5a only ever exercised the STARTUP-time parser
(``discovery.py::_parse_order_by_spec`` on a contract's own declared
``order_by`` field); the REQUEST-time application of a caller-supplied value
into the real ``SnapshotCache``/sort path was never wired or tested.

This test drives the REAL FastAPI route with a REAL ``SnapshotCache``
(seeded via the same ``apply_message`` the live Kafka consumer loop calls,
same pattern as ``tests/integration/test_projection_bus_seam.py``) -- no
MagicMock stand-in for the cache -- so the assertions below exercise the
actual ``_sort_rows`` comparison, not merely a kwarg captured by a mock.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

_TOPIC = "onex.snapshot.projection.test-omn16290-order-by.v1"
_SOURCE_TOPIC = "onex.evt.platform.node-heartbeat.v1"

_CFG = ProjectionTableConfig(
    topic=_TOPIC,
    table="test_omn16290_table",
    columns=("id", "rank", "created_at"),
    order_by="created_at DESC",
    order_by_spec=(("created_at", "DESC", None),),
    bus_backed=True,
    key_columns=("id",),
    limit=100,
)


def _delta_bytes(*, row_id: str, rank: int, created_at: str, offset: int) -> bytes:
    payload = {
        "topic": _TOPIC,
        "key": [row_id],
        "op": "upsert",
        "row": {"id": row_id, "rank": rank, "created_at": created_at},
        "observed_at": created_at,
        "source_event_id": f"evt-{row_id}-{offset}",
        "source_topic": _SOURCE_TOPIC,
        "source_partition": 0,
        "source_offset": offset,
        "projection_version": "projection_snapshot.v1",
    }
    return json.dumps(payload).encode("utf-8")


def _seeded_cache() -> SnapshotCache:
    """A REAL SnapshotCache (no mock) with 3 rows applied OUT of both rank
    order and creation order, so a test that merely echoed insertion order
    back would fail every ordering assertion below."""
    cache = SnapshotCache(
        {_TOPIC: _CFG},
        bootstrap_servers="unused:9092",
        # Explicit override (OMN-15840): this test exercises request-time
        # order_by, not the default group-id derivation, which requires
        # ONEX_ENVIRONMENT.
        group_id="test-omn16290-order-by-group",
    )
    # Applied in the order: rank=2, rank=0, rank=1 -- deliberately not sorted
    # by rank OR by created_at, so any ordering observed downstream is proof
    # of an actual sort, not an accident of insertion/dict order.
    cache.apply_message(
        _TOPIC,
        key=b"row-2",
        value=_delta_bytes(
            row_id="row-2", rank=2, created_at="2026-08-20T10:00:02+00:00", offset=1
        ),
        headers=[("tenant_id", b"omninode")],
    )
    cache.apply_message(
        _TOPIC,
        key=b"row-0",
        value=_delta_bytes(
            row_id="row-0", rank=0, created_at="2026-08-20T10:00:00+00:00", offset=2
        ),
        headers=[("tenant_id", b"omninode")],
    )
    cache.apply_message(
        _TOPIC,
        key=b"row-1",
        value=_delta_bytes(
            row_id="row-1", rank=1, created_at="2026-08-20T10:00:01+00:00", offset=3
        ),
        headers=[("tenant_id", b"omninode")],
    )
    # No live consumer in this test -- bootstrap is marked complete directly,
    # mirroring what a caught-up partition assignment does (matches the
    # established pattern in test_projection_bus_seam.py).
    cache._state[_TOPIC].bootstrap_complete = True
    assert cache.row_count(_TOPIC) == 3
    return cache


@contextmanager
def _client_for(cache: SnapshotCache) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    app.dependency_overrides[get_topic_map] = lambda: {_TOPIC: _CFG}
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def _rank_sequence(body: dict[str, Any]) -> list[int]:
    return [row["rank"] for row in body["rows"]]


@pytest.mark.unit
class TestOrderByRequestTimeReordersRealRows:
    def test_default_ordering_is_the_contract_declared_column(self) -> None:
        """Baseline: absent order_by serves the contract default
        (created_at DESC) -- establishes the CONTRAST the order_by=rank
        assertions below depend on."""
        with _client_for(_seeded_cache()) as client:
            resp = client.get(f"/projection/{_TOPIC}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ordering"] == "created_at DESC"
        # created_at DESC == rank DESC in this fixture (rank tracks
        # creation order) -- i.e. NOT the order_by=rank ASC result below.
        assert _rank_sequence(body) == [2, 1, 0]

    def test_order_by_non_default_column_actually_reorders_served_rows(self) -> None:
        """The core OMN-16290 regression: a non-default request-time
        order_by must change the REAL returned row sequence, driven through
        the real SnapshotCache sort path -- not just the reported 'ordering'
        string."""
        with _client_for(_seeded_cache()) as client:
            resp = client.get(f"/projection/{_TOPIC}?order_by=rank+ASC")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ordering"] == "rank ASC"
        assert _rank_sequence(body) == [0, 1, 2]
        assert [row["id"] for row in body["rows"]] == ["row-0", "row-1", "row-2"]

    def test_order_by_direction_is_honoured_in_real_row_order(self) -> None:
        with _client_for(_seeded_cache()) as client:
            resp = client.get(f"/projection/{_TOPIC}?order_by=rank+DESC")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ordering"] == "rank DESC"
        assert _rank_sequence(body) == [2, 1, 0]

    def test_order_by_bare_column_defaults_ascending(self) -> None:
        with _client_for(_seeded_cache()) as client:
            resp = client.get(f"/projection/{_TOPIC}?order_by=rank")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ordering"] == "rank ASC"
        assert _rank_sequence(body) == [0, 1, 2]

    def test_order_by_unknown_column_rejected_never_silent_fallback(self) -> None:
        """no-defensive-defaults: an invalid order_by must be REJECTED
        (422), never silently served under the contract default ordering."""
        with _client_for(_seeded_cache()) as client:
            resp = client.get(f"/projection/{_TOPIC}?order_by=not_a_real_column")
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "invalid_order_by"
        assert body["topic"] == _TOPIC

    def test_order_by_malformed_clause_rejected(self) -> None:
        with _client_for(_seeded_cache()) as client:
            resp = client.get(f"/projection/{_TOPIC}?order_by=rank+SIDEWAYS+EXTRA")
        assert resp.status_code == 422
        assert resp.json()["error"] == "invalid_order_by"
