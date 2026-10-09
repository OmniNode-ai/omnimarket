# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19721 regression coverage for runtime-error fingerprint projection."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _topics_for_handler_entry,
)

from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
    HandlerProjectionRuntimeErrorFingerprints,
)
from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_runtime_error_fingerprint_runner import (
    RuntimeErrorFingerprintProjectionWriter,
)
from omnimarket.nodes.node_projection_runtime_error_fingerprints.models import (
    ModelRuntimeErrorEventWire,
    ModelRuntimeErrorFingerprintRequest,
)

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_runtime_error_fingerprints"
)
_CONTRACT_PATH = _NODE_DIR / "contract.yaml"
_SOURCE_TOPIC = "onex.evt.omnibase-infra.runtime-error.v1"
_EMITTED_AT = datetime(2026, 9, 26, 11, 40, 27, 191578, tzinfo=UTC)
_EVENT_ID = "11111111-1111-4111-8111-111111111111"


def _bridge_payload() -> dict[str, Any]:
    """Exact field set emitted by RuntimeLogEventBridge."""
    return {
        "event_id": _EVENT_ID,
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "logger_family": "omnibase_infra.runtime.service_kernel",
        "log_level": "ERROR",
        "message_template": "Dispatcher {} failed: ValidationError",
        "raw_message": "Dispatcher projection failed: ValidationError",
        "error_category": "runtime",
        "severity": "error",
        "fingerprint": "producer-fingerprint",
        "occurrence_count_local": 1,
        "exception_type": "ValidationError",
        "exception_message": "event Field required",
        "stack_trace": "traceback",
        "hostname": "omninode-runtime-201",
        "service_label": "omnibase_infra",
        "emitted_at": _EMITTED_AT.isoformat(),
    }


class _IdempotentRecordingAdapter:
    """Small asyncpg-shaped double for the writer's read/upsert sequence."""

    def __init__(self) -> None:
        self.row: dict[str, Any] | None = None
        self.connected = False
        self.upserts: list[tuple[Any, ...]] = []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self.connected
        if "SELECT occurrence_count" in query:
            if self.row is None:
                return []
            return [
                {
                    "occurrence_count": self.row["occurrence_count"],
                    "first_seen_at": self.row["first_seen_at"],
                    "last_seen_at": self.row["last_seen_at"],
                }
            ]

        assert "ON CONFLICT (fingerprint)" in query
        assert "last_applied_event_id" in query
        assert "IS DISTINCT FROM EXCLUDED.last_applied_event_id" in query
        self.upserts.append(params)

        incoming = {
            "fingerprint": params[0],
            "logger_name": params[1],
            "error_category": params[2],
            "category_evidence": params[3],
            "severity": params[4],
            "message_template": params[5],
            "exception_type": params[6],
            "occurrence_count": params[7],
            "correlation_id": params[8],
            "service_name": params[9],
            "hostname": params[10],
            "first_seen_at": params[11],
            "last_seen_at": params[12],
            "last_applied_event_id": params[13],
            "projection_cursor": 1,
        }
        if self.row is None:
            self.row = incoming
        else:
            is_new_event = (
                self.row["last_applied_event_id"] != incoming["last_applied_event_id"]
            )
            self.row["occurrence_count"] += (
                incoming["occurrence_count"] if is_new_event else 0
            )
            self.row["first_seen_at"] = min(
                self.row["first_seen_at"], incoming["first_seen_at"]
            )
            if incoming["last_seen_at"] >= self.row["last_seen_at"]:
                for field in (
                    "error_category",
                    "category_evidence",
                    "severity",
                    "exception_type",
                    "correlation_id",
                    "service_name",
                    "hostname",
                ):
                    self.row[field] = incoming[field]
            self.row["last_seen_at"] = max(
                self.row["last_seen_at"], incoming["last_seen_at"]
            )
            self.row["last_applied_event_id"] = incoming["last_applied_event_id"]

        return [
            {
                key: value
                for key, value in self.row.items()
                if key != "last_applied_event_id"
            }
        ]


async def _discard_snapshot(*args: Any, **kwargs: Any) -> bool:
    return True


def test_bridge_payload_uses_emitted_at_as_seen_time() -> None:
    event = ModelRuntimeErrorEventWire.model_validate(_bridge_payload())
    result = HandlerProjectionRuntimeErrorFingerprints().handle(
        ModelRuntimeErrorFingerprintRequest(event=event)
    )

    assert result.row.first_seen_at == _EMITTED_AT
    assert result.row.last_seen_at == _EMITTED_AT


def test_redelivering_one_event_id_counts_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = RuntimeErrorFingerprintProjectionWriter()
    adapter = _IdempotentRecordingAdapter()
    monkeypatch.setattr(writer, "_db", adapter)
    monkeypatch.setattr(writer, "publish_snapshot_delta", _discard_snapshot)

    first = writer.handle(_bridge_payload())
    second = writer.handle(_bridge_payload())

    assert first["fingerprint_rows"][0]["occurrence_count"] == 1
    assert second["fingerprint_rows"][0]["occurrence_count"] == 1
    assert adapter.row is not None
    assert adapter.row["occurrence_count"] == 1
    assert [params[13] for params in adapter.upserts] == [_EVENT_ID, _EVENT_ID]


def test_unavailable_snapshot_publish_refuses_success_then_retries_without_counting_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted row alone cannot acknowledge the promised snapshot delta."""
    writer = RuntimeErrorFingerprintProjectionWriter()
    adapter = _IdempotentRecordingAdapter()
    monkeypatch.setattr(writer, "_db", adapter)
    monkeypatch.setattr(writer, "_ensure_producer", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError, match="snapshot was not published"):
        writer.handle(_bridge_payload())
    assert not adapter.connected
    assert adapter.row is not None
    assert adapter.row["occurrence_count"] == 1

    producer = AsyncMock()
    monkeypatch.setattr(writer, "_ensure_producer", AsyncMock(return_value=producer))
    result = writer.handle(_bridge_payload())

    assert result["rows_upserted"] == 1
    assert result["fingerprint_rows"][0]["occurrence_count"] == 1
    producer.send_and_wait.assert_awaited_once()
    published = producer.send_and_wait.await_args
    assert published is not None
    assert published.args[0] == writer._contract["projection_api"]["topic"]
    delta = json.loads(published.kwargs["value"])
    assert (
        published.kwargs["key"].decode() == result["fingerprint_rows"][0]["fingerprint"]
    )
    assert delta["row"] == result["fingerprint_rows"][0]
    assert delta["source_event_id"] == _EVENT_ID
    assert delta["row"]["correlation_id"] == _bridge_payload()["correlation_id"]
    assert delta["row"]["last_seen_at"] == _EMITTED_AT.isoformat()


def test_successive_runtime_errors_advance_the_correlated_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two writer calls publish one fingerprint with the latest trace and time."""
    writer = RuntimeErrorFingerprintProjectionWriter()
    monkeypatch.setattr(writer, "_db", _IdempotentRecordingAdapter())
    producer = AsyncMock()
    monkeypatch.setattr(writer, "_ensure_producer", AsyncMock(return_value=producer))
    first = writer.handle({**_bridge_payload(), "_partition": 2, "_offset": 41})
    newer = {
        **_bridge_payload(),
        "event_id": "33333333-3333-4333-8333-333333333333",
        "correlation_id": "44444444-4444-4444-8444-444444444444",
        "emitted_at": (_EMITTED_AT + timedelta(seconds=1)).isoformat(),
        "_partition": 2,
        "_offset": 42,
    }
    second = writer.handle(newer)
    deltas = [
        json.loads(call.kwargs["value"])
        for call in producer.send_and_wait.await_args_list
    ]
    assert len(deltas) == 2
    assert deltas[0]["row"] == first["fingerprint_rows"][0]
    assert deltas[1]["row"] == second["fingerprint_rows"][0]
    assert deltas[0]["key"] == deltas[1]["key"]
    assert [delta["row"]["occurrence_count"] for delta in deltas] == [1, 2]
    assert deltas[1]["row"]["last_seen_at"] > deltas[0]["row"]["last_seen_at"]
    assert deltas[1]["row"]["correlation_id"] == newer["correlation_id"]
    assert [delta["source_partition"] for delta in deltas] == [2, 2]
    assert [delta["source_offset"] for delta in deltas] == [41, 42]


def test_runtime_dispatch_resolves_only_the_writer() -> None:
    contract = _parse_contract(
        contract_path=_CONTRACT_PATH,
        entry_point_name="node_projection_runtime_error_fingerprints",
        package_name="omnimarket",
        package_version="test",
    )
    assert contract.handler_routing is not None

    entries = contract.handler_routing.handlers
    assert [entry.handler.name for entry in entries] == [
        "RuntimeErrorFingerprintProjectionWriter"
    ]
    assert _topics_for_handler_entry(contract, entries[0]) == (_SOURCE_TOPIC,)
    assert all(
        entry.handler.name != "HandlerProjectionRuntimeErrorFingerprints"
        for entry in entries
    )
