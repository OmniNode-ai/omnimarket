# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared item identity, producer sequence and barrier fields (OMN-18531, TA.21).

omniclaude's two capture hooks stamp each record with ``stream`` and
``producer_seq``, each content record with the ``content_record_id`` of the
lineage reference it fulfils, and journal a ``barrier`` content record per
stream. The redaction contract fails closed: a field nobody declared is
replaced by its digest, which would turn the join key, the sequence number and
the barrier's high-water into hashes no reader can compare. These tests hold
the declared fields verbatim, and the undeclared control to its digest.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_event_emit_effect.redaction import (
    EnumCaptureClass,
    load_contract,
    redact_capture,
)

HOOK_TOPIC = "onex.evt.omniclaude.hook-event.v1"
CONTENT_TOPIC = "onex.cmd.omniintelligence.content-captured.v1"

_RECORD_ID = "3f9a2c1e-0000-5000-8000-000000000042"
_SESSION = "11111111-2222-3333-4444-555555555555"

_CONTENT_FIELDS = (
    "content_record_id",
    "producer_seq",
    "stream",
    "record_type",
    "barrier_id",
    "high_water",
    "content_capture",
)


def _content_record() -> dict[str, Any]:
    return {
        "session_id": _SESSION,
        "content_kind": "prompt",
        "content": "plain words",
        "chunk_index": 0,
        "chunk_count": 1,
        "content_record_id": _RECORD_ID,
        "producer_seq": 7,
        "stream": "content",
    }


def _barrier(stream: str) -> dict[str, Any]:
    barrier: dict[str, Any] = {
        "content_kind": "barrier",
        "record_type": "barrier",
        "session_id": _SESSION,
        "barrier_id": "b-1",
        "stream": stream,
        "high_water": 12,
        "emitted_at": "2026-10-15T06:00:00+00:00",
    }
    if stream == "lineage":
        barrier["content_capture"] = "disabled"
    return barrier


@pytest.mark.unit
def test_identity_sequence_and_barrier_fields_are_declared_verbatim() -> None:
    fields = load_contract().topics[CONTENT_TOPIC].fields
    for name in _CONTENT_FIELDS:
        assert fields[name] is EnumCaptureClass.CAPTURE_VERBATIM, name
    hook_fields = load_contract().topics[HOOK_TOPIC].fields
    for name in ("producer_seq", "stream"):
        assert hook_fields[name] is EnumCaptureClass.CAPTURE_VERBATIM, name


@pytest.mark.unit
def test_a_content_record_keeps_its_join_key_and_sequence() -> None:
    out = redact_capture(_content_record(), CONTENT_TOPIC)
    assert out["content_record_id"] == _RECORD_ID
    assert out["producer_seq"] == 7
    assert out["stream"] == "content"


@pytest.mark.unit
@pytest.mark.parametrize("stream", ["lineage", "content"])
def test_a_barrier_record_keeps_its_high_water_and_flag(stream: str) -> None:
    sent = _barrier(stream)
    out = redact_capture(sent, CONTENT_TOPIC)
    for name in ("barrier_id", "stream", "high_water", "record_type"):
        assert out[name] == sent[name], name
    assert out.get("content_capture") == sent.get("content_capture")


@pytest.mark.unit
def test_a_lineage_event_keeps_its_producer_sequence() -> None:
    event = {
        "event_id": _RECORD_ID,
        "hook_event_name": "Stop",
        "session_id": _SESSION,
        "stream": "lineage",
        "producer_seq": 3,
    }
    out = redact_capture(event, HOOK_TOPIC)
    assert out["producer_seq"] == 3
    assert out["stream"] == "lineage"


@pytest.mark.unit
def test_control_an_undeclared_sibling_is_still_hashed() -> None:
    """Positive control: the verbatim result above is the contract, not a no-op."""
    out = redact_capture({**_content_record(), "producer_sequence": 7}, CONTENT_TOPIC)
    assert str(out["producer_sequence"]).startswith("sha256:")
