# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19721 regression coverage for runtime-error fingerprint projection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    writer._db = adapter  # type: ignore[assignment]
    monkeypatch.setattr(writer, "publish_snapshot_delta", _discard_snapshot)

    first = writer.handle(_bridge_payload())
    second = writer.handle(_bridge_payload())

    assert first["fingerprint_rows"][0]["occurrence_count"] == 1
    assert second["fingerprint_rows"][0]["occurrence_count"] == 1
    assert adapter.row is not None
    assert adapter.row["occurrence_count"] == 1
    assert [params[13] for params in adapter.upserts] == [_EVENT_ID, _EVENT_ID]


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
