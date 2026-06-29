# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests: StructuredEventLogger emits schema-valid ModelStructuredLogEntry payloads.

DoD assertion for OMN-13703: every payload published by StructuredEventLogger is
parseable as ModelStructuredLogEntry from omnibase_core.

No live infra required. All tests are @pytest.mark.unit.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_log_entry_status import EnumLogEntryStatus
from omnibase_core.enums.enum_log_level import EnumLogLevel
from omnibase_core.enums.enum_redaction_state import EnumRedactionState
from omnibase_core.enums.enum_suppression_decision import EnumSuppressionDecision
from omnibase_core.models.logging.model_structured_log_entry import (
    ModelStructuredLogEntry,
)

from omnimarket.logging.structured_logger import LOG_ENTRY_TOPIC, StructuredEventLogger

# ---------------------------------------------------------------------------
# Minimal async event bus stub — no live infra
# ---------------------------------------------------------------------------


class _CaptureBus:
    """Minimal async event bus stub that captures published payloads."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, *, key: bytes | None, value: bytes) -> None:
        self.published.append((topic, value))


@pytest.mark.unit
class TestStructuredEventLoggerSchemaValidity:
    """Every emitted payload must be schema-valid against ModelStructuredLogEntry."""

    @pytest.mark.asyncio
    async def test_info_emits_schema_valid_payload(self) -> None:
        bus = _CaptureBus()
        logger = StructuredEventLogger("node_x", event_bus=bus)
        await logger.info("hello world", operation="build")

        assert len(bus.published) == 1
        topic, raw = bus.published[0]
        assert topic == LOG_ENTRY_TOPIC

        data = json.loads(raw.decode())
        entry = ModelStructuredLogEntry.model_validate(data)

        assert entry.source_system == "node_x"
        assert entry.operation == "build"
        assert entry.level == EnumLogLevel.INFO
        assert entry.message == "hello world"
        assert entry.status == EnumLogEntryStatus.EMITTED
        assert entry.redaction_state == EnumRedactionState.NONE
        assert entry.suppression_decision == EnumSuppressionDecision.EMIT
        assert isinstance(entry.entry_id, UUID)

    @pytest.mark.asyncio
    async def test_error_emits_schema_valid_payload(self) -> None:
        bus = _CaptureBus()
        logger = StructuredEventLogger("node_err", event_bus=bus)
        await logger.error("something failed", operation="execute")

        data = json.loads(bus.published[0][1].decode())
        entry = ModelStructuredLogEntry.model_validate(data)

        assert entry.level == EnumLogLevel.ERROR
        assert entry.source_system == "node_err"

    @pytest.mark.asyncio
    async def test_debug_warning_critical_all_valid(self) -> None:
        bus = _CaptureBus()
        logger = StructuredEventLogger("node_all", event_bus=bus)
        await logger.debug("dbg")
        await logger.warning("warn")
        await logger.critical("crit")

        assert len(bus.published) == 3
        for _, raw in bus.published:
            data = json.loads(raw.decode())
            ModelStructuredLogEntry.model_validate(data)

    @pytest.mark.asyncio
    async def test_uuid_correlation_id_preserved(self) -> None:
        bus = _CaptureBus()
        cid = uuid4()
        logger = StructuredEventLogger("node_trace", event_bus=bus)
        await logger.info("traced", correlation_id=cid)

        data = json.loads(bus.published[0][1].decode())
        entry = ModelStructuredLogEntry.model_validate(data)

        assert entry.correlation_id == cid

    @pytest.mark.asyncio
    async def test_string_uuid_correlation_id_coerced(self) -> None:
        bus = _CaptureBus()
        cid = uuid4()
        logger = StructuredEventLogger("node_trace", event_bus=bus)
        await logger.info("traced", correlation_id=str(cid))

        data = json.loads(bus.published[0][1].decode())
        entry = ModelStructuredLogEntry.model_validate(data)

        assert entry.correlation_id == cid

    @pytest.mark.asyncio
    async def test_non_uuid_correlation_id_becomes_none(self) -> None:
        """Non-UUID correlation strings are silently dropped (ModelStructuredLogEntry.correlation_id is UUID|None)."""
        bus = _CaptureBus()
        logger = StructuredEventLogger("node_trace", event_bus=bus)
        await logger.info("traced", correlation_id="not-a-uuid")

        data = json.loads(bus.published[0][1].decode())
        entry = ModelStructuredLogEntry.model_validate(data)

        assert entry.correlation_id is None

    @pytest.mark.asyncio
    async def test_duration_ms_in_metadata(self) -> None:
        bus = _CaptureBus()
        logger = StructuredEventLogger("node_perf", event_bus=bus)
        await logger.info("perf", duration_ms=123.4)

        data = json.loads(bus.published[0][1].decode())
        entry = ModelStructuredLogEntry.model_validate(data)

        assert entry.metadata["duration_ms"] == "123.4"

    @pytest.mark.asyncio
    async def test_extra_metadata_preserved(self) -> None:
        bus = _CaptureBus()
        logger = StructuredEventLogger("node_meta", event_bus=bus)
        await logger.info("with meta", phase="compile", attempt="3")

        data = json.loads(bus.published[0][1].decode())
        entry = ModelStructuredLogEntry.model_validate(data)

        assert entry.metadata["phase"] == "compile"
        assert entry.metadata["attempt"] == "3"

    @pytest.mark.asyncio
    async def test_no_bus_returns_entry_without_publishing(self) -> None:
        """StructuredEventLogger with no bus still builds a valid entry."""
        logger = StructuredEventLogger("node_silent")
        entry = await logger.info("silent message")

        assert isinstance(entry, ModelStructuredLogEntry)
        assert entry.message == "silent message"

    @pytest.mark.asyncio
    async def test_artifact_refs_forwarded(self) -> None:
        bus = _CaptureBus()
        logger = StructuredEventLogger("node_art", event_bus=bus)
        entry = logger._build_entry(
            EnumLogLevel.INFO,
            "with refs",
            artifact_refs=["art-abc", "art-def"],
        )
        await logger._emit(entry)

        data = json.loads(bus.published[0][1].decode())
        validated = ModelStructuredLogEntry.model_validate(data)
        assert validated.artifact_refs == ["art-abc", "art-def"]
