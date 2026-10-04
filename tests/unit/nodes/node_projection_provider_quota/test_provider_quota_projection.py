# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Database-free quota fold and writer boundary tests.

The adapter fake models timestamp refusal and call-count windows only. These
tests check the writer's bindings and response to that adapter contract; the
real-Postgres suite remains the proof of SQL execution and concurrent merges.
There is no decrement event in the observation contract.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.events.provider_quota import PROVIDER_WIDE_MODEL_SCOPE
from omnimarket.nodes.node_projection_provider_quota.handlers import (
    HandlerProjectionProviderQuota,
    ProviderQuotaProjectionWriter,
)
from omnimarket.nodes.node_projection_provider_quota.models import (
    ModelProviderQuotaProjectionRequest,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_AT = datetime(2026, 10, 3, tzinfo=UTC)


def _event(**updates: Any) -> dict[str, Any]:
    return {
        "event_id": "22222222-2222-2222-2222-222222222222",
        "tenant_id": str(_TENANT),
        "credential_ref": "test.credential",
        "provider_id": "host:unknown.example",
        "model_name": "unknown-model",
        "outcome": "call_ok",
        "http_status": 200,
        "call_started_at": _AT - timedelta(seconds=1),
        "observed_at": _AT,
        "window_seconds": 60,
        "source": "inprocess_effect",
    } | updates


class _MemoryQuotaDB:
    """Small adapter contract fake; deliberately does not execute SQL."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.connected = False
        self.connections = 0
        self.closes = 0
        self.fail_on_write: int | None = None

    async def connect(self) -> None:
        assert not self.connected
        self.connected = True
        self.connections += 1

    async def close(self) -> None:
        assert self.connected
        self.connected = False
        self.closes += 1

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.connected
        assert tenant == str(params[0])
        assert len(params) == 16
        # Tie the fake's limited semantics to the production SQL contract.
        sql = " ".join(query.split())
        assert "WHERE t.observed_at < EXCLUDED.observed_at" in sql
        assert "calls_total = t.calls_total + 1" in sql
        assert (
            "hits_total = t.hits_total + CASE WHEN $10::boolean THEN 1 ELSE 0 END"
            in sql
        )
        assert (
            ">= t.window_started_at + make_interval(secs => EXCLUDED.window_seconds)"
            in sql
        )
        assert "RETURNING projection_cursor" in sql
        self.calls.append(params)
        if len(self.calls) == self.fail_on_write:
            raise RuntimeError("quota adapter write failed")

        key = params[:4]
        observed_at, window_seconds = params[4:6]
        counts_hit = params[9]
        previous = self.rows.get(key)
        if previous is not None and observed_at <= previous["observed_at"]:
            return []
        if previous is None:
            row = {
                "calls_total": 0,
                "hits_total": 0,
                "window_started_at": observed_at,
                "window_calls": 0,
            }
        else:
            row = dict(previous)
            if observed_at >= row["window_started_at"] + timedelta(
                seconds=window_seconds
            ):
                row["window_started_at"] = observed_at
                row["window_calls"] = 0
        row["calls_total"] += 1
        row["hits_total"] += int(counts_hit)
        row["window_calls"] += 1
        row["observed_at"] = observed_at
        row["window_seconds"] = window_seconds
        row["projection_cursor"] = len(self.calls)
        self.rows[key] = row
        return [{"projection_cursor": row["projection_cursor"]}]


@pytest.fixture
def writer(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ProviderQuotaProjectionWriter, _MemoryQuotaDB]:
    projection = ProviderQuotaProjectionWriter()
    store = _MemoryQuotaDB()
    monkeypatch.setattr(projection, "_db", store)
    return projection, store


def test_unknown_provider_initializes_both_scopes_and_keeps_input(
    writer: tuple[ProviderQuotaProjectionWriter, _MemoryQuotaDB],
) -> None:
    projection, store = writer
    event = _event(_partition="3", _offset="8", _fallback_id="fallback")
    original = deepcopy(event)
    assert projection.topics == projection.subscribe_topics
    result = projection.handle(event)

    assert event == original
    assert result == {
        "rows_upserted": 2,
        "quota_rows": [
            {
                "tenant_id": str(_TENANT),
                "credential_ref": "test.credential",
                "provider_id": "host:unknown.example",
                "model_scope": scope,
                "projection_cursor": cursor,
            }
            for scope, cursor in (("unknown-model", 1), (PROVIDER_WIDE_MODEL_SCOPE, 2))
        ],
    }
    assert set(store.rows) == {
        (_TENANT, "test.credential", "host:unknown.example", scope)
        for scope in ("unknown-model", PROVIDER_WIDE_MODEL_SCOPE)
    }
    for row in store.rows.values():
        assert (row["calls_total"], row["hits_total"], row["window_calls"]) == (1, 0, 1)
    assert store.connections == store.closes == 1
    assert not store.connected


@pytest.mark.parametrize(
    ("updates", "error_type"),
    [
        ({"outcome": "decrement"}, "enum"),
        ({"window_seconds": -1}, "greater_than_equal"),
        ({"window_seconds": 0}, "greater_than_equal"),
        ({"tenant_id": None}, "uuid_type"),
        ({"observed_at": None}, "datetime_type"),
        ({"outcome": "limit_hit"}, "value_error"),
        ({"blocked_until": _AT}, "value_error"),
    ],
)
def test_invalid_or_decrement_payload_cannot_reduce_zero_hits_or_write(
    writer: tuple[ProviderQuotaProjectionWriter, _MemoryQuotaDB],
    updates: dict[str, Any],
    error_type: str,
) -> None:
    projection, store = writer
    projection.handle(_event())
    original = deepcopy(store.rows)
    calls_before = len(store.calls)

    with pytest.raises(ValidationError) as raised:
        projection.handle(_event(**updates))

    assert raised.value.errors()[0]["type"] == error_type
    assert store.rows == original
    assert len(store.calls) == calls_before
    assert all(row["hits_total"] == 0 for row in store.rows.values())
    assert store.connections == store.closes == 2
    assert not store.connected


@pytest.mark.parametrize("seconds_earlier", [0, 1])
def test_replay_or_older_observation_does_not_change_state(
    writer: tuple[ProviderQuotaProjectionWriter, _MemoryQuotaDB],
    seconds_earlier: int,
) -> None:
    projection, store = writer
    event = _event(
        outcome="limit_hit",
        http_status=429,
        block_scope="provider",
        blocked_until=_AT + timedelta(seconds=60),
        disposition="cooldown",
    )
    assert projection.handle(event)["rows_upserted"] == 2
    original = deepcopy(store.rows)
    replay = event | {"observed_at": _AT - timedelta(seconds=seconds_earlier)}
    if seconds_earlier:
        replay["event_id"] = str(UUID(int=3))
    assert projection.handle(replay) == {"rows_upserted": 0, "quota_rows": []}
    assert store.rows == original
    assert len(store.calls) == 4  # Both rows still reach the atomic upsert.
    assert all(
        row["calls_total"] == row["hits_total"] == 1 for row in store.rows.values()
    )
    assert store.connections == store.closes == 2


@pytest.mark.parametrize("window_seconds", [10, 60])
def test_reset_window_rolls_over_at_exact_boundary(
    writer: tuple[ProviderQuotaProjectionWriter, _MemoryQuotaDB],
    window_seconds: int,
) -> None:
    projection, store = writer
    for seconds in (0, window_seconds - 1, window_seconds):
        at = _AT + timedelta(seconds=seconds)
        result = projection.handle(
            _event(
                event_id=str(UUID(int=seconds + 1)),
                observed_at=at,
                call_started_at=at,
                window_seconds=window_seconds,
            )
        )
        assert result["rows_upserted"] == 2
        for row in store.rows.values():
            if seconds < window_seconds:
                assert row["window_started_at"] == _AT
                assert row["window_calls"] == (1 if seconds == 0 else 2)
            else:
                assert row["window_started_at"] == at
                assert row["window_calls"] == 1
                assert row["calls_total"] == 3
                assert row["hits_total"] == 0
            assert row["window_seconds"] == window_seconds


@pytest.mark.parametrize("scope", ["model", "provider"])
def test_writer_binds_hit_and_indefinite_block_only_to_selected_scope(
    writer: tuple[ProviderQuotaProjectionWriter, _MemoryQuotaDB], scope: str
) -> None:
    projection, store = writer
    projection.handle(
        _event(
            outcome="limit_hit",
            http_status=429,
            provider_code="capacity",
            block_scope=scope,
            disposition="disable_until_billing",
            blocked_indefinitely=True,
            reason="quota unavailable",
        )
    )
    for params in store.calls:
        sets_block = params[3] == (
            "unknown-model" if scope == "model" else PROVIDER_WIDE_MODEL_SCOPE
        )
        assert params[4:11] == (_AT, 60, "limit_hit", 429, "capacity", True, sets_block)
        assert params[11:] == (
            "disable_until_billing" if sets_block else None,
            None,
            sets_block,
            "quota unavailable" if sets_block else None,
            None,
        )


@pytest.mark.parametrize("outcome", ["call_ok", "call_failed"])
def test_fold_clears_blocks_only_for_successful_call(outcome: str) -> None:
    request = ModelProviderQuotaProjectionRequest.model_validate(
        _event(outcome=outcome)
    )
    rows = HandlerProjectionProviderQuota().handle(request).rows
    assert len(rows) == 2
    for row in rows:
        assert not row.counts_hit
        assert not row.sets_block
        assert row.disposition is None
        assert row.blocked_until is None
        assert not row.blocked_indefinitely
        assert row.block_reason is None
        assert row.clears_blocks_before == (
            _AT - timedelta(seconds=1) if outcome == "call_ok" else None
        )


async def test_async_projection_accepts_empty_replay_result(
    writer: tuple[ProviderQuotaProjectionWriter, _MemoryQuotaDB],
) -> None:
    projection, store = writer
    event = _event()
    event["_envelope_timestamp"] = event.pop("observed_at")
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="fallback", topic=projection.topics[0]
    )
    await store.connect()
    try:
        assert await projection.project_event(meta.topic, event, meta) is True
        original = deepcopy(store.rows)
        assert await projection.project_event(meta.topic, event, meta) is True
        assert store.rows == original
        assert len(store.calls) == 4
    finally:
        await store.close()


def test_write_failure_propagates_and_closes_adapter(
    writer: tuple[ProviderQuotaProjectionWriter, _MemoryQuotaDB],
) -> None:
    projection, store = writer
    store.fail_on_write = 1
    with pytest.raises(RuntimeError, match="quota adapter write failed"):
        projection.handle(_event())
    assert store.rows == {}
    assert len(store.calls) == 1
    assert store.connections == store.closes == 1
    assert not store.connected
