# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24458: a windowed projection read, and a Valkey page cache with a declared expiry.

Measured on the dev lane on 2026-11-23: the dashboard's overview read of
``delegation.decisions.v1`` asked for no window, so the store ran the ascending
walk over the whole tenant history (a parallel sequential scan of 137,818 rows,
2,000 of them kept, the OLDEST ones) whether the page showed 24 hours or 7 days.

Failure modes these tests are written against:

* W1 a request with ``window=7d`` reads a row older than the window, or its
  statement has no window predicate or no row cap;
* W2 a ``window=24h`` request reads as many rows as a ``window=7d`` request;
* W3 a windowed page is handed to the walk's ascending order, so its first
  rows are the oldest of the window instead of the newest;
* C1 a second request inside the expiry reads the store again, or a request
  after the expiry is served from the cache;
* C2 the expiry is a number written in handler code instead of the contract's
  ``page_cache.ttl_seconds``;
* C3 a Valkey that raises serves an empty page, an invented page or an error
  instead of the windowed store read with ``cache.fallback`` set;
* C4 a failed or refused read is held in the cache;
* S1 a read just past the expiry waits for the store read instead of being
  answered from the held page (the dev lane's cold reads of 2.1 s to 13.5 s on
  2026-11-24 were exactly these waits);
* S2 a stale page is served without saying it is stale, or is never refreshed;
* S3 two stale reads, or two runtimes, each start a store read for one page (one
  claim in the shared Valkey decides);
* S4 a refresh that fails drops the held page, or leaves the refresh claim
  held so no later read can refresh it;
* S5 a page older than the contract's stale window is served.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnimarket.handlers.projection_read_page_window import parse_window_seconds
from omnimarket.nodes.node_projection_query_api_effect.handlers import (
    handler_projection_query,
)
from omnimarket.nodes.node_projection_query_api_effect.handlers.handler_projection_query import (
    HandlerProjectionQuery,
)
from omnimarket.nodes.node_projection_query_api_effect.models import (
    ModelProjectionQueryRequest,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.contract_routes import (
    contract_page_cache,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.page_cache import (
    PageCacheUnavailableError,
    ValkeyPageCache,
    build_page_cache,
    page_cache_key,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import read_projection_page
from omnimarket.projection.table_reader import build_window_query

pytestmark = pytest.mark.unit

_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_NOW = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)
_COLUMNS = (
    "correlation_id",
    "tenant_id",
    "model_name",
    "written_at",
    "projection_cursor",
)


def _decisions_cfg() -> ProjectionTableConfig:
    """``delegation.decisions``: a cursor, a freshness column, one row per key, tenant-scoped."""
    return ProjectionTableConfig(
        topic=_DECISIONS,
        table="delegation_events",
        schema_name="public",
        relation_schema="public",
        columns=_COLUMNS,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", _COLUMNS),
        freshness_column="written_at",
        cursor_column="projection_cursor",
        limit=500,
        bus_backed=True,
        key_columns=("correlation_id",),
        key_grain="mutable",
        tenant_column="tenant_id",
    )


class _FakeStore:
    """A store of one run per hour for twenty days that applies the bound it is handed.

    ``rows`` answers what Postgres would answer for the statement the real
    source builds: with no ``window_seconds`` it is the ascending walk (the
    oldest ``limit * 4`` rows by cursor); with one it is the newest rows
    inside the window, at most ``row_cap``. ``scanned`` is what each call read.
    """

    backing = "table"

    def __init__(self, hours: int = 20 * 24) -> None:
        self.history = [
            {
                "correlation_id": f"run-{n:05d}",
                "tenant_id": _TENANT,
                "model_name": "m",
                "written_at": (_NOW - timedelta(hours=hours - n)).isoformat(),
                "projection_cursor": n,
            }
            for n in range(hours)
        ]
        self.calls: list[dict[str, Any]] = []
        self.scanned: list[int] = []

    def unavailable(self, topic: str) -> tuple[str, str] | None:
        return None

    async def registry_tenant_uuid(self, cfg: Any, tenant_slug: str) -> UUID | None:
        return None

    async def rows(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: Any,
        tenant_id: str | None,
        since: str | None = None,
        correlation_id: str | None = None,
        selection: str = "newest",
        window_seconds: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append({"window_seconds": window_seconds, "row_cap": row_cap})
        if window_seconds is None:
            served = self.history[: cfg.limit * 4]
        else:
            floor = _NOW - timedelta(seconds=window_seconds)
            inside = [
                row
                for row in self.history
                if datetime.fromisoformat(str(row["written_at"])) >= floor
            ]
            inside.reverse()
            served = inside if row_cap is None else inside[:row_cap]
        self.scanned.append(len(served))
        return [dict(row) for row in served]

    async def walk_origin(self, cfg: Any, *, tenant_id: str | None) -> str | None:
        return "-1"

    async def latest_event_at(
        self, cfg: Any, *, tenant_id: str | None, window_rows: Any = None
    ) -> datetime | None:
        return _NOW

    def staleness(self, topic: str, latest_ts: str | None) -> dict[str, object]:
        return {}


def _read(store: _FakeStore, **kwargs: Any) -> Any:
    return asyncio.run(
        read_projection_page(
            _DECISIONS,
            topic_map={_DECISIONS: _decisions_cfg()},
            source=store,
            tenant=_TENANT,
            **kwargs,
        )
    )


def test_w1_seven_day_request_reads_only_rows_inside_the_window() -> None:
    store = _FakeStore()
    page = _read(store, window="7d", limit=100)
    assert page.status_code == 200
    floor = _NOW - timedelta(days=7)
    assert store.calls == [{"window_seconds": 7 * 86400, "row_cap": 101}]
    stamps = [datetime.fromisoformat(r["written_at"]) for r in page.body["rows"]]
    assert stamps
    assert all(stamp >= floor for stamp in stamps)
    assert page.body["window"] == {
        "requested": "7d",
        "seconds": 7 * 86400,
        "rows_read": 101,
    }
    assert page.body["truncated"] is True
    assert page.body["next_cursor"] is None


def test_w2_a_day_reads_fewer_rows_than_a_week_and_neither_walks_the_history() -> None:
    store = _FakeStore()
    day = _read(store, window="24h", limit=500)
    week = _read(store, window="7d", limit=500)
    assert day.body["window"]["rows_read"] < week.body["window"]["rows_read"]
    assert day.body["window"]["rows_read"] == 24
    assert week.body["window"]["rows_read"] == 7 * 24
    assert max(store.scanned) < len(store.history)


def test_w3_a_windowed_page_serves_the_newest_runs_first() -> None:
    store = _FakeStore()
    page = _read(store, window="7d", limit=10)
    stamps = [datetime.fromisoformat(r["written_at"]) for r in page.body["rows"]]
    assert stamps == sorted(stamps, reverse=True)
    assert stamps[0] == _NOW - timedelta(hours=1)


def test_w4_no_window_is_the_unchanged_walk_with_no_window_key() -> None:
    store = _FakeStore()
    page = _read(store, limit=10)
    assert store.calls == [{"window_seconds": None, "row_cap": None}]
    assert "window" not in page.body


def test_w5_a_window_that_is_not_hours_or_days_is_refused_before_any_read() -> None:
    store = _FakeStore()
    for bad in ("7", "0d", "d", "7w", "-1d", "1.5d"):
        page = _read(store, window=bad)
        assert page.status_code == 422, bad
        assert page.body["error"] == "invalid_window"
    assert store.calls == []
    assert parse_window_seconds("24H") == 86400


def test_w6_the_statement_carries_the_window_predicate_the_cap_and_the_newest_first_order() -> (
    None
):
    cfg = _decisions_cfg()
    spec = tuple(cfg.order_by_spec)
    query = build_window_query(
        cfg,
        order_spec=spec,
        tenant_id=_TENANT,
        relation_columns=frozenset(_COLUMNS),
        unique_keys=(frozenset({"correlation_id"}),),
        not_null_columns=frozenset({"correlation_id", "projection_cursor"}),
        window_seconds=7 * 86400,
        row_cap=101,
    )
    assert "WITH RECURSIVE" not in query.sql
    assert (
        "\"written_at\" >= now() - ($2::double precision * interval '1 second')"
        in query.sql
    )
    assert 'ORDER BY "written_at" DESC LIMIT 101' in query.sql
    assert query.sql.endswith('ORDER BY "written_at" DESC')
    assert query.params == (_TENANT, 7.0 * 86400)
    control = build_window_query(
        cfg,
        order_spec=spec,
        tenant_id=_TENANT,
        relation_columns=frozenset(_COLUMNS),
        unique_keys=(frozenset({"correlation_id"}),),
        not_null_columns=frozenset({"correlation_id", "projection_cursor"}),
        selection="walk",
    )
    assert "interval" not in control.sql


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0


class _FakeValkey:
    """The client's get and set with a clock, so a held page expires when its ``ex`` says."""

    def __init__(self, clock: _Clock, *, down: bool = False) -> None:
        self._clock = clock
        self.down = down
        self.held: dict[str, tuple[float, str]] = {}
        self.ttls: list[int] = []
        self.deleted: list[str] = []
        self.fail_claims = False

    def now(self) -> float:
        return self._clock.now

    async def get(self, key: str) -> str | None:
        if self.down:
            raise ConnectionError("valkey is down")
        found = self.held.get(key)
        if found is None or found[0] <= self._clock.now:
            return None
        return found[1]

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if self.down or (self.fail_claims and nx):
            raise ConnectionError("valkey is down")
        assert ex is not None
        if nx:
            found = self.held.get(key)
            if found is not None and found[0] > self._clock.now:
                return None
        else:
            self.ttls.append(ex)
        self.held[key] = (self._clock.now + ex, value)
        return True

    async def delete(self, key: str) -> int:
        if self.down:
            raise ConnectionError("valkey is down")
        self.deleted.append(key)
        return 1 if self.held.pop(key, None) is not None else 0


def _handler(store: _FakeStore, client: _FakeValkey) -> HandlerProjectionQuery:
    settings = contract_page_cache()
    return HandlerProjectionQuery(
        topic_map={_DECISIONS: _decisions_cfg()},
        row_source=store,
        page_cache=ValkeyPageCache(settings, client, clock=client.now),
        page_cache_key_prefix=settings.key_prefix,
    )


def _ask(handler: HandlerProjectionQuery, **kwargs: Any) -> Any:
    request = ModelProjectionQueryRequest(
        route_id="projection_page", topic=_DECISIONS, tenant=_TENANT, **kwargs
    )
    return asyncio.run(handler.handle(request))


def test_c1_a_second_request_inside_the_expiry_is_served_from_the_cache() -> None:
    clock, store = _Clock(), _FakeStore()
    client = _FakeValkey(clock)
    handler = _handler(store, client)
    ttl = contract_page_cache().ttl_seconds

    first = _ask(handler, window="7d", limit=100)
    clock.now = ttl - 1
    second = _ask(handler, window="7d", limit=100)
    assert first.body["cache"]["state"] == "miss"
    assert second.body["cache"] == {"state": "hit", "ttl_seconds": ttl}
    assert len(store.calls) == 1
    assert second.body["rows"] == first.body["rows"]

    settings = contract_page_cache()
    clock.now = ttl + settings.stale_seconds + 1
    third = _ask(handler, window="7d", limit=100)
    assert third.body["cache"]["state"] == "miss"
    assert len(store.calls) == 2
    assert client.ttls == [ttl + settings.stale_seconds] * 2


def test_c1b_a_different_window_is_a_different_page() -> None:
    clock, store = _Clock(), _FakeStore()
    handler = _handler(store, _FakeValkey(clock))
    _ask(handler, window="7d", limit=100)
    other = _ask(handler, window="24h", limit=100)
    assert other.body["cache"]["state"] == "miss"
    assert len(store.calls) == 2


def test_c2_the_expiry_is_the_contracts_and_no_handler_writes_one() -> None:
    contract = yaml_contract()
    assert contract["page_cache"]["ttl_seconds"] == contract_page_cache().ttl_seconds
    assert contract_page_cache().ttl_seconds >= 1
    for module in (handler_projection_query,):
        text = inspect.getsource(module)
        assert "ttl_seconds=" not in text
        assert "ex=" not in text
    assert build_page_cache({}) is None


def yaml_contract() -> dict[str, Any]:
    import yaml

    path = (
        Path(handler_projection_query.__file__).resolve().parent.parent
        / "contract.yaml"
    )
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_c3_a_valkey_outage_serves_the_windowed_read_and_says_so() -> None:
    clock, store = _Clock(), _FakeStore()
    handler = _handler(store, _FakeValkey(clock, down=True))
    answer = _ask(handler, window="7d", limit=100)
    assert answer.status_code == 200
    assert answer.body["cache"]["state"] == "unavailable"
    assert answer.body["cache"]["fallback"] is True
    assert answer.body["window"]["rows_read"] == 101
    assert len(answer.body["rows"]) == 100
    assert store.calls == [{"window_seconds": 7 * 86400, "row_cap": 101}]
    assert not json.dumps(answer.body).count("valkey is down")


def test_c4_a_refused_read_is_not_held() -> None:
    clock, store = _Clock(), _FakeStore()
    client = _FakeValkey(clock)
    handler = _handler(store, client)
    answer = _ask(handler, window="7w", limit=100)
    assert answer.status_code == 422
    assert client.held == {}


def test_c5_the_cache_adapter_names_an_outage_it_does_not_swallow() -> None:
    clock = _Clock()
    cache = ValkeyPageCache(contract_page_cache(), _FakeValkey(clock, down=True))
    with pytest.raises(PageCacheUnavailableError):
        asyncio.run(cache.get("k"))
    with pytest.raises(PageCacheUnavailableError):
        asyncio.run(cache.put("k", {"a": 1}))
    with pytest.raises(PageCacheUnavailableError):
        asyncio.run(cache.claim_refresh("k"))
    with pytest.raises(PageCacheUnavailableError):
        asyncio.run(cache.release_refresh("k"))


def test_c6_the_key_changes_with_every_input_that_changes_the_rows() -> None:
    base = ModelProjectionQueryRequest(
        route_id="projection_page", topic=_DECISIONS, window="7d", limit=100
    )
    same = base.model_copy()
    assert page_cache_key("p", base) == page_cache_key("p", same)
    for changed in (
        base.model_copy(update={"window": "24h"}),
        base.model_copy(update={"limit": 50}),
        base.model_copy(update={"tenant": _TENANT}),
        base.model_copy(update={"since": "5"}),
    ):
        assert page_cache_key("p", changed) != page_cache_key("p", base)


def test_c7_the_environment_names_the_valkey_and_a_missing_host_means_no_cache() -> (
    None
):
    settings = contract_page_cache()
    made: list[tuple[str, int, int]] = []

    def factory(url: str, connect: int, operation: int) -> _FakeValkey:
        made.append((url, connect, operation))
        return _FakeValkey(_Clock())

    cache = build_page_cache(
        {
            settings.valkey.host_env: "valkey",
            settings.valkey.port_env: "6379",
            str(settings.valkey.password_env): "p@ss/word",
        },
        client_factory=factory,
    )
    assert cache is not None
    assert cache.ttl_seconds == settings.ttl_seconds
    url, connect, operation = made[0]
    assert url == f"redis://:p%40ss%2Fword@valkey:6379/{settings.valkey.database}"
    assert (connect, operation) == (
        settings.valkey.connect_timeout_seconds,
        settings.valkey.operation_timeout_seconds,
    )
    assert build_page_cache({settings.valkey.host_env: "valkey"}) is None


# --- stale while refreshing (OMN-24458) -----------------------------------------------------


class _GatedStore(_FakeStore):
    """A store whose reads wait on a gate, so a test can hold a refresh open or let it fail."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None
        self.fail = False

    async def rows(self, cfg: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("store read failed")
        return await super().rows(cfg, **kwargs)


def _request() -> ModelProjectionQueryRequest:
    return ModelProjectionQueryRequest(
        route_id="projection_page",
        topic=_DECISIONS,
        tenant=_TENANT,
        window="7d",
        limit=100,
    )


def _key() -> str:
    return page_cache_key(contract_page_cache().key_prefix, _request())


async def _settle(handler: HandlerProjectionQuery) -> None:
    await asyncio.gather(*handler.pending_refreshes())


def test_s1_a_stale_page_is_answered_without_waiting_for_the_store() -> None:
    async def scenario() -> None:
        clock, store = _Clock(), _GatedStore()
        handler = _handler(store, _FakeValkey(clock))
        await handler.handle(_request())
        assert len(store.calls) == 1
        clock.now = contract_page_cache().ttl_seconds + 5
        store.gate = asyncio.Event()  # the store would never answer
        answer = await asyncio.wait_for(handler.handle(_request()), timeout=2)
        assert answer.status_code == 200
        assert answer.body["cache"]["state"] == "stale"
        assert len(store.calls) == 1
        store.gate.set()
        await _settle(handler)
        assert len(store.calls) == 2

    asyncio.run(scenario())


def test_s2_the_stale_answer_names_its_age_and_the_refresh_makes_the_next_read_a_hit() -> (
    None
):
    async def scenario() -> None:
        clock, store = _Clock(), _GatedStore()
        client = _FakeValkey(clock)
        handler = _handler(store, client)
        settings = contract_page_cache()
        first = await handler.handle(_request())
        clock.now = settings.ttl_seconds + 5
        stale = await handler.handle(_request())
        assert stale.body["cache"] == {
            "state": "stale",
            "ttl_seconds": settings.ttl_seconds,
            "stale_seconds": settings.stale_seconds,
            "age_seconds": settings.ttl_seconds + 5,
            "refreshing": True,
        }
        assert stale.body["rows"] == first.body["rows"]
        await _settle(handler)
        fresh = await handler.handle(_request())
        assert fresh.body["cache"] == {
            "state": "hit",
            "ttl_seconds": settings.ttl_seconds,
        }
        assert len(store.calls) == 2
        assert client.deleted == [f"{_key()}:refresh"]

    asyncio.run(scenario())


def test_s3_one_page_is_refreshed_once_across_reads_and_runtimes() -> None:
    async def scenario() -> None:
        clock, store = _Clock(), _GatedStore()
        client = _FakeValkey(clock)
        handler = _handler(store, client)
        await handler.handle(_request())
        clock.now = contract_page_cache().ttl_seconds + 5
        store.gate = asyncio.Event()
        for _ in range(3):
            assert (await handler.handle(_request())).body["cache"]["state"] == "stale"
            # the fake Valkey never suspends; a real one does, which lets the refresh start
            await asyncio.sleep(0)
        store.gate.set()
        await _settle(handler)
        assert (
            len(store.calls) == 2
        )  # one first read, one refresh for three stale reads

        # a second runtime on the same Valkey, with the claim already held elsewhere
        clock.now += contract_page_cache().ttl_seconds + 5
        other = _handler(store, client)
        client.held[f"{_key()}:refresh"] = (clock.now + 30, "1")
        answer = await other.handle(_request())
        await _settle(other)
        assert answer.body["cache"]["state"] == "stale"
        assert len(store.calls) == 2

    asyncio.run(scenario())


def test_s4_a_failed_refresh_keeps_the_held_page_and_releases_the_claim() -> None:
    async def scenario() -> None:
        clock, store = _Clock(), _GatedStore()
        client = _FakeValkey(clock)
        handler = _handler(store, client)
        first = await handler.handle(_request())
        clock.now = contract_page_cache().ttl_seconds + 5
        store.fail = True
        stale = await handler.handle(_request())
        await _settle(handler)
        assert stale.body["cache"]["state"] == "stale"
        assert client.deleted == [f"{_key()}:refresh"]
        again = await handler.handle(_request())
        assert again.body["cache"]["state"] == "stale"
        assert again.body["rows"] == first.body["rows"]
        await _settle(handler)
        store.fail = False
        await handler.handle(_request())
        await _settle(handler)
        assert (await handler.handle(_request())).body["cache"]["state"] == "hit"

    asyncio.run(scenario())


def test_s5_a_page_past_the_stale_window_is_recomputed_before_it_is_answered() -> None:
    async def scenario() -> None:
        clock, store = _Clock(), _GatedStore()
        handler = _handler(store, _FakeValkey(clock))
        settings = contract_page_cache()
        await handler.handle(_request())
        clock.now = settings.ttl_seconds + settings.stale_seconds - 1
        assert (await handler.handle(_request())).body["cache"]["state"] == "stale"
        await _settle(handler)
        clock.now = 2 * (settings.ttl_seconds + settings.stale_seconds)
        late = await handler.handle(_request())
        assert late.body["cache"]["state"] == "miss"
        assert handler.pending_refreshes() == ()

    asyncio.run(scenario())


def test_s6_a_claim_that_cannot_be_taken_leaves_the_page_stale_and_still_answered() -> (
    None
):
    async def scenario() -> None:
        clock, store = _Clock(), _GatedStore()
        client = _FakeValkey(clock)
        handler = _handler(store, client)
        await handler.handle(_request())
        clock.now = contract_page_cache().ttl_seconds + 5
        client.fail_claims = True
        stale = await handler.handle(_request())
        await _settle(handler)
        assert stale.status_code == 200
        assert stale.body["cache"]["state"] == "stale"
        assert len(store.calls) == 1

    asyncio.run(scenario())


def test_s7_the_stale_window_and_the_claim_time_are_the_contracts() -> None:
    contract = yaml_contract()["page_cache"]
    settings = contract_page_cache()
    assert contract["stale_seconds"] == settings.stale_seconds >= 1
    assert contract["refresh_lock_seconds"] == settings.refresh_lock_seconds >= 1
    text = inspect.getsource(handler_projection_query)
    assert "stale_seconds=" not in text
    assert "ex=" not in text


def test_s8_a_page_stored_by_the_earlier_shape_is_read_as_nothing() -> None:
    clock = _Clock()
    client = _FakeValkey(clock)
    cache = ValkeyPageCache(contract_page_cache(), client, clock=client.now)
    client.held["old"] = (100.0, json.dumps({"rows": [], "topic": "t"}))
    assert asyncio.run(cache.get("old")) is None
