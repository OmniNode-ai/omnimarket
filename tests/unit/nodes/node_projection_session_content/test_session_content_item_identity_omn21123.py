# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_projection_session_content takes the TA.21 item identity stamp (OMN-21123).

The capture producer stamps each content record with ``content_record_id``,
``stream`` and ``producer_seq``, and journals one barrier record per stream on
the same topic. The inbound model types those fields, and the row key keeps two
stamped records apart: one barrier call writes both streams' barriers under one
``emitted_at``, which before the stamp joined the key derived one row for both.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_capture_item_identity_effect.handlers.handler_item_identity import (
    build_barrier_payload,
)
from omnimarket.nodes.node_projection_session_content.handlers import (
    handler_session_content as writer_module,
)
from omnimarket.nodes.node_projection_session_content.handlers.session_content_fold import (
    SessionContentFoldError,
    fold_session_content,
)
from omnimarket.nodes.node_projection_session_content.models.model_session_content import (
    ModelSessionContentRecord,
)

pytestmark = pytest.mark.unit

SESSION = "11111111-2222-3333-4444-555555555555"
EMITTED_AT = "2026-10-18T00:00:00+00:00"


def _barrier(stream: str, high_water: int) -> dict[str, Any]:
    return build_barrier_payload(
        session_id=SESSION,
        barrier_id="barrier-1",
        stream=stream,
        high_water_value=high_water,
        content_capture_enabled=True,
        emitted_at=EMITTED_AT,
    )


def _prompt(**stamp: Any) -> dict[str, Any]:
    return {
        "session_id": SESSION,
        "turn_id": "turn-1",
        "content_kind": "prompt",
        "content": "same words",
        "content_sha256": "a" * 64,
        "emitted_at": EMITTED_AT,
        **stamp,
    }


def test_both_streams_barriers_of_one_call_fold_to_two_rows() -> None:
    lineage = ModelSessionContentRecord.model_validate(_barrier("lineage", 6))
    content = ModelSessionContentRecord.model_validate(_barrier("content", 4))
    assert lineage.stream == "lineage"
    assert lineage.high_water == 6
    assert lineage.content_capture == "enabled"
    assert (
        fold_session_content(lineage).event_id != fold_session_content(content).event_id
    )


def test_two_stamped_records_differing_only_in_their_stamp_fold_to_two_rows() -> None:
    first = ModelSessionContentRecord.model_validate(
        _prompt(content_record_id="c-1", stream="content", producer_seq=1)
    )
    second = ModelSessionContentRecord.model_validate(
        _prompt(content_record_id="c-2", stream="content", producer_seq=2)
    )
    assert fold_session_content(first).event_id != fold_session_content(second).event_id


def test_an_unstamped_record_keeps_the_key_it_always_had() -> None:
    record = ModelSessionContentRecord.model_validate(_prompt())
    # The pre-change key of this record, computed at origin/dev 42476d681.
    assert (
        fold_session_content(record).event_id
        == "7662be304c59f71957c4cb59b5a3bea310488fdc945ffe772ab3b6e00cf59903"
    )


@pytest.mark.parametrize(
    "stamp",
    [
        {"stream": "other"},
        {"producer_seq": 0},
        {"producer_seq": "7"},
        {"producer_seq": True},
        {"high_water": -1},
    ],
)
def test_a_malformed_stamp_is_refused(stamp: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelSessionContentRecord.model_validate(_prompt(**stamp))


def test_a_barrier_without_its_high_water_is_refused() -> None:
    payload = _barrier("content", 4)
    del payload["high_water"]
    with pytest.raises(SessionContentFoldError):
        fold_session_content(ModelSessionContentRecord.model_validate(payload))


@pytest.mark.parametrize("stream", ["lineage", "content"])
def test_compaction_pack_build_barrier_persists_barrier_stamps(stream: str) -> None:
    row = fold_session_content(
        ModelSessionContentRecord.model_validate(_barrier(stream, 6))
    )
    persisted = dict(
        zip(writer_module._COLUMNS, writer_module._parameters(row), strict=True)
    )
    assert persisted["session_id"] == SESSION
    assert persisted["content_kind"] == "barrier"
    assert persisted["record_type"] == "barrier"
    assert persisted["stream"] == stream
    assert persisted["barrier_id"] == "barrier-1"
    assert persisted["high_water"] == 6
    assert persisted["content_capture"] == ("enabled" if stream == "lineage" else None)
    assert persisted["emitted_at"].isoformat() == EMITTED_AT


def test_compaction_pack_build_barrier_persists_content_sequence() -> None:
    row = fold_session_content(
        ModelSessionContentRecord.model_validate(
            _prompt(content_record_id="c-1", stream="content", producer_seq=7)
        )
    )
    persisted = dict(
        zip(writer_module._COLUMNS, writer_module._parameters(row), strict=True)
    )
    assert persisted["producer_seq"] == 7
    assert persisted["stream"] == "content"
    assert persisted["content_record_id"] == "c-1"


def test_compaction_pack_build_barrier_keeps_legacy_content_stamps_nullable() -> None:
    row = fold_session_content(ModelSessionContentRecord.model_validate(_prompt()))
    persisted = dict(
        zip(writer_module._COLUMNS, writer_module._parameters(row), strict=True)
    )
    for name in (
        "content_record_id",
        "stream",
        "producer_seq",
        "record_type",
        "barrier_id",
        "high_water",
        "content_capture",
    ):
        assert persisted[name] is None
