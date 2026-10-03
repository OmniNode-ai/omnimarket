# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Disposition accounting at the writer boundary, without PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.events.delegation_disposition import (
    ModelDelegationDispositionRecorded,
)
from omnimarket.nodes.node_projection_delegation_disposition.handlers import (
    HandlerDelegationDispositionWriter,
)
from omnimarket.nodes.node_projection_delegation_disposition.models import (
    ModelDelegationDispositionProjectionRequest,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_RUN = UUID("22222222-2222-2222-2222-222222222222")
_RECORDED_AT = datetime(2026, 9, 30, tzinfo=UTC)


def _event(**updates: Any) -> dict[str, Any]:
    return ModelDelegationDispositionRecorded.build(
        **(
            {
                "tenant_id": _TENANT,
                "delegation_correlation_id": _RUN,
                "caller_lane": "accounting-lane",
                "disposition": "accepted_as_is",
                "reason_code": "correct_as_is",
                "artifact_kind": "commit",
                "artifact_ref": "a" * 40,
                "engine": "codex",
                "recorded_at": _RECORDED_AT,
            }
            | updates
        )
    ).model_dump(mode="json")


class _DispositionStore:
    """Record writes and emulate the SQL's monotonic conflict result only.

    There is deliberately no delegation_events relation. This unit double
    checks the emitted SQL guard; PostgreSQL semantics have separate coverage.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, UUID], dict[str, Any]] = {}
        self.statements: list[str] = []
        self.connected = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connected += 1

    async def close(self) -> None:
        self.closed += 1

    async def execute(
        self, query: str, *params: Any, tenant: str
    ) -> list[dict[str, UUID]]:
        sql = " ".join(query.split())
        assert sql.startswith("INSERT INTO public.delegation_dispositions (")
        assert "ON CONFLICT (tenant_id, delegation_correlation_id) DO UPDATE SET" in sql
        for column in ("disposition", "reason_code", "recorded_at", "disposition_id"):
            assert f"{column} = EXCLUDED.{column}" in sql
        assert (
            "WHERE (public.delegation_dispositions.recorded_at, "
            "public.delegation_dispositions.disposition_id) "
            "< (EXCLUDED.recorded_at, EXCLUDED.disposition_id)"
        ) in sql
        assert sql.endswith("RETURNING disposition_id")
        self.statements.append(sql)
        columns = query.split("(", 1)[1].split(")", 1)[0].split(",")
        row = dict(zip((column.strip() for column in columns), params, strict=True))
        assert tenant == str(row["tenant_id"])
        key = row["tenant_id"], row["delegation_correlation_id"]
        previous = self.rows.get(key)
        if previous is not None and (
            previous["recorded_at"],
            previous["disposition_id"],
        ) >= (row["recorded_at"], row["disposition_id"]):
            return []
        self.rows[key] = row
        return [{"disposition_id": row["disposition_id"]}]


@pytest.fixture
def accounting_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HandlerDelegationDispositionWriter, _DispositionStore]:
    writer = HandlerDelegationDispositionWriter()
    store = _DispositionStore()
    monkeypatch.setattr(writer, "_db", store)
    return writer, store


def _assert_accounted(
    result: dict[str, Any], event: dict[str, Any], *, upserted: int
) -> None:
    assert result == {
        "rows_handled": 1,
        "rows_upserted": upserted,
        "rows_refused_by_ordering_guard": 1 - upserted,
        "disposition_rows": [
            {
                field: event[field]
                for field in (
                    "tenant_id",
                    "delegation_correlation_id",
                    "disposition_id",
                )
            }
        ],
    }


@pytest.mark.parametrize(
    ("disposition", "reason_code"),
    [
        pytest.param("accepted_as_is", "correct_as_is", id="used-correct-as-is"),
        pytest.param(
            "accepted_as_is",
            "verified_against_source",
            id="used-verified-against-source",
        ),
        ("edited", "minor_fix"),
        ("edited", "partial_use"),
        ("edited", "reformatted"),
        pytest.param("rejected", "wrong_answer", id="discarded-wrong-answer"),
        pytest.param("rejected", "hallucinated", id="discarded-hallucinated"),
        pytest.param("rejected", "off_task", id="discarded-off-task"),
        pytest.param("rejected", "incomplete", id="discarded-incomplete"),
        pytest.param("rejected", "unusable_format", id="discarded-unusable-format"),
        ("ignored", "not_needed"),
        ("ignored", "superseded"),
        ("ignored", "engine_failed"),
        ("ignored", "lane_decided_first"),
    ],
)
def test_each_valid_disposition_reason_counts_one_handled_and_upserted_row(
    accounting_writer: tuple[HandlerDelegationDispositionWriter, _DispositionStore],
    disposition: str,
    reason_code: str,
) -> None:
    writer, store = accounting_writer
    event = _event(
        disposition=disposition,
        reason_code=reason_code,
        edit_ratio=0.25 if disposition == "edited" else None,
    )

    _assert_accounted(writer.handle(event), event, upserted=1)

    row = store.rows[(_TENANT, _RUN)]
    assert row["disposition"] == disposition
    assert row["reason_code"] == reason_code
    assert row["edit_ratio"] == event["edit_ratio"]
    assert row["recorded_at"] == _RECORDED_AT
    assert (store.connected, store.closed) == (1, 1)


@pytest.mark.parametrize(
    ("disposition", "valid_reason", "invalid_reason"),
    [
        pytest.param("accepted_as_is", "correct_as_is", "minor_fix", id="used"),
        ("edited", "minor_fix", "wrong_answer"),
        pytest.param("rejected", "wrong_answer", "not_needed", id="discarded"),
        ("ignored", "not_needed", "correct_as_is"),
    ],
)
def test_reason_from_another_disposition_is_refused_before_any_row_is_written(
    accounting_writer: tuple[HandlerDelegationDispositionWriter, _DispositionStore],
    disposition: str,
    valid_reason: str,
    invalid_reason: str,
) -> None:
    writer, store = accounting_writer
    event = _event(
        disposition=disposition,
        reason_code=valid_reason,
        edit_ratio=0.25 if disposition == "edited" else None,
    )
    event["reason_code"] = invalid_reason

    with pytest.raises(
        ValidationError, match="reason_code does not belong to disposition"
    ):
        writer.handle(event)

    assert store.rows == {}
    assert store.statements == []
    assert (store.connected, store.closed) == (1, 1)


def test_run_without_a_delegation_event_still_counts_its_disposition(
    accounting_writer: tuple[HandlerDelegationDispositionWriter, _DispositionStore],
) -> None:
    writer, store = accounting_writer
    orphan_run = UUID("33333333-3333-3333-3333-333333333333")
    event = _event(delegation_correlation_id=orphan_run)

    _assert_accounted(writer.handle(event), event, upserted=1)

    assert set(store.rows) == {(_TENANT, orphan_run)}
    assert len(store.statements) == 1
    assert "delegation_events" not in store.statements[0]


def test_newer_disposition_replaces_same_run_without_creating_another_row(
    accounting_writer: tuple[HandlerDelegationDispositionWriter, _DispositionStore],
) -> None:
    writer, store = accounting_writer
    first = _event()
    newer = _event(
        disposition="edited",
        reason_code="partial_use",
        edit_ratio=0.25,
        recorded_at=_RECORDED_AT + timedelta(seconds=1),
    )

    _assert_accounted(writer.handle(first), first, upserted=1)
    _assert_accounted(writer.handle(newer), newer, upserted=1)

    assert len(store.rows) == 1
    row = store.rows[(_TENANT, _RUN)]
    assert row["disposition"] == "edited"
    assert row["reason_code"] == "partial_use"
    assert str(row["disposition_id"]) == newer["disposition_id"]


@pytest.mark.parametrize("age_seconds", [0, -1], ids=["duplicate", "older"])
def test_duplicate_or_older_disposition_counts_as_handled_but_not_upserted(
    accounting_writer: tuple[HandlerDelegationDispositionWriter, _DispositionStore],
    age_seconds: int,
) -> None:
    writer, store = accounting_writer
    first = _event()
    _assert_accounted(writer.handle(first), first, upserted=1)
    before = dict(store.rows)
    refused = (
        first
        if age_seconds == 0
        else _event(
            disposition="ignored",
            reason_code="superseded",
            recorded_at=_RECORDED_AT + timedelta(seconds=age_seconds),
        )
    )

    _assert_accounted(writer.handle(refused), refused, upserted=0)

    assert store.rows == before
    assert len(store.rows) == 1
    assert (store.connected, store.closed) == (2, 2)


def test_missing_caller_lane_is_refused_without_inventing_an_accounting_lane(
    accounting_writer: tuple[HandlerDelegationDispositionWriter, _DispositionStore],
) -> None:
    writer, store = accounting_writer
    event = _event()
    del event["caller_lane"]

    with pytest.raises(ValidationError) as exc_info:
        writer.handle(event)

    assert any(
        error["loc"] == ("caller_lane",) and error["type"] == "missing"
        for error in exc_info.value.errors()
    )
    assert store.rows == {}
    assert store.statements == []
    assert (store.connected, store.closed) == (1, 1)


async def test_consumer_acknowledges_a_duplicate_as_handled_without_another_upsert(
    accounting_writer: tuple[HandlerDelegationDispositionWriter, _DispositionStore],
) -> None:
    writer, store = accounting_writer
    event = _event()
    meta = MessageMeta(partition=0, offset=0, fallback_id="")

    assert await writer.project_event(writer.topics[0], event, meta) is True
    before = dict(store.rows)
    assert await writer.project_event(writer.topics[0], event, meta) is True

    assert store.rows == before
    assert len(store.statements) == 2
    assert (store.connected, store.closed) == (0, 0)


def test_non_mapping_payload_cannot_create_a_disposition_accounting_row() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ModelDelegationDispositionProjectionRequest.model_validate([])

    assert exc_info.value.errors()[0]["type"] == "model_type"
