# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13151 migration-compat tests for ModelContentDiscoveredEvent.

The event gains durable-ingestion fields (content_blob_ref, source_type,
scope_ref, source_version, tags, priority_hint, artifact_kind). Every new field
is optional with a default, so a payload serialized by the pre-OMN-13151 schema
must still parse and round-trip unchanged.
"""

from __future__ import annotations

from uuid import uuid4

from omnimarket.nodes.node_filesystem_crawler_effect.models.model_content_discovered_event import (
    EnumContentSourceType,
    ModelContentDiscoveredEvent,
)

_OLD_PAYLOAD = {
    "event_id": str(uuid4()),
    "correlation_id": str(uuid4()),
    "emitted_at_utc": "2026-06-22T00:00:00+00:00",
    "source_ref": "fs://root",
    "content_type": "text/plain",
    "content_fingerprint": "sha256:deadbeef",
    "file_size_bytes": 42,
    "mtime": 1_700_000_000.0,
    "root_path": "/repo",
    "relative_path": "src/main.py",
}


def test_old_payload_parses_without_new_fields() -> None:
    """A pre-OMN-13151 payload (no new keys) deserializes with defaults."""
    event = ModelContentDiscoveredEvent.model_validate(_OLD_PAYLOAD)

    # New fields default to back-compatible values.
    assert event.content_blob_ref is None
    assert event.source_type is EnumContentSourceType.FILESYSTEM
    assert event.scope_ref is None
    assert event.source_version is None
    assert event.tags == ()
    assert event.priority_hint is None
    assert event.artifact_kind is None


def test_old_payload_round_trips() -> None:
    """Old payload survives a serialize/deserialize round trip unchanged."""
    event = ModelContentDiscoveredEvent.model_validate(_OLD_PAYLOAD)
    restored = ModelContentDiscoveredEvent.model_validate_json(event.model_dump_json())
    assert restored == event


def test_new_fields_round_trip() -> None:
    """A payload populating every new field round-trips with correct types."""
    payload = {
        **_OLD_PAYLOAD,
        "content_blob_ref": "blob://artifacts/abc123",
        "source_type": "tool_output",
        "scope_ref": "session-7",
        "source_version": "commit-9f8e7d",
        "tags": ["build", "stderr"],
        "priority_hint": 7,
        "artifact_kind": "tool_output",
    }
    event = ModelContentDiscoveredEvent.model_validate(payload)

    assert event.content_blob_ref == "blob://artifacts/abc123"
    assert event.source_type is EnumContentSourceType.TOOL_OUTPUT
    assert event.scope_ref == "session-7"
    assert event.source_version == "commit-9f8e7d"
    assert event.tags == ("build", "stderr")
    assert event.priority_hint == 7
    assert event.artifact_kind == "tool_output"

    restored = ModelContentDiscoveredEvent.model_validate_json(event.model_dump_json())
    assert restored == event


def test_all_expanded_source_types_accept() -> None:
    """Every member of the expanded source taxonomy is accepted."""
    for member in EnumContentSourceType:
        event = ModelContentDiscoveredEvent.model_validate(
            {**_OLD_PAYLOAD, "source_type": member.value}
        )
        assert event.source_type is member
