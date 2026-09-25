# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19513: session-started carries a REDACTED working directory, and the
work ledger must project it rather than reject it.

The governed capture redaction contract (OMN-17209,
``node_event_emit_effect/contracts/capture_redaction.yaml``) classifies
``session-started.working_directory`` as ``capture_shape_only``, so the wire
carries ``{"type": "str", "length": N}`` instead of the directory name. The
inbound model typed the field ``str | None``, so every session-started record
raised a ValidationError in ``handle()``. Measured on the .201 dev lane
2026-09-25T01:02Z: no session-started row in ``omninode_internal.work_events``
since 2026-09-21T20:45:04Z while the other three hook classes stayed current,
and the consumer group read Stable with lag 0 the whole time.

``_LIVE_SESSION_STARTED`` is a record read verbatim off
``onex.evt.omniclaude.session-started.v1`` on that broker (offset near the
2026-09-25T00:15Z head). It carries no directory content by construction, which
is the point.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_work_events.handlers.handler_projection_work_events import (
    TABLE,
    TOPIC_SESSION_STARTED,
    HandlerProjectionWorkEvents,
)
from omnimarket.nodes.node_projection_work_events.models.model_work_event import (
    ModelWorkEventInbound,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage

_LIVE_SESSION_STARTED = (
    '{"actor": "sha256:389432cbf83f3d1b64029a75864ec96f8fd06451b4c99d7b28385d395d480bfa", '
    '"hook_source": "startup", "lane": "", "lane_source": "unresolved", "lane_ticket": "", '
    '"session_id": "35443637-b75a-4288-8a27-71a484b1dd25", '
    '"turn_id": "sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b", '
    '"working_directory": {"type": "str", "length": 9}, "workspace_path": ".", '
    '"hook_fired_at": "2026-09-25T00:15:07.746783+00:00", '
    '"correlation_id": "35443637-b75a-4288-8a27-71a484b1dd25", "causation_id": null, '
    '"emitted_at": "2026-09-25T00:15:09.434255+00:00", '
    '"entity_id": "35443637-b75a-4288-8a27-71a484b1dd25", "schema_version": "1.0.0", '
    '"redaction_state": "redacted"}'
)

# A directory name that must never surface once reduced to its shape. Used as a
# sentinel: it is fed through the plain-string path for the control, and must
# be absent from every artefact of the shape path.
_SENTINEL_DIRECTORY = "omni_home"


class _RecordingPublisher:
    """Records the snapshot deltas ``handle()`` republishes after a write."""

    def __init__(self) -> None:
        self.sent: list[ModelSnapshotDeltaMessage] = []

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        self.sent.append(message)
        return True


@pytest.mark.unit
def test_redacted_working_directory_live_wire_record_is_projected() -> None:
    """AC1. RED on origin/dev: the live record raised a string_type error."""
    db = InmemoryDatabaseAdapter()
    publisher = _RecordingPublisher()
    result = HandlerProjectionWorkEvents(publisher=publisher).handle(
        {
            **json.loads(_LIVE_SESSION_STARTED),
            "_db": db,
            "_topic": TOPIC_SESSION_STARTED,
        }
    )

    assert result["rows_upserted"] == 1
    rows = db.query(TABLE)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_kind"] == "session.started"
    assert row["actor_id"] == "35443637-b75a-4288-8a27-71a484b1dd25"
    assert row["source_topic"] == TOPIC_SESSION_STARTED
    # The shape is kept on the row as the metadata it is, so the row still
    # records that a directory was present and how long its label was.
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload["working_directory"] == {"type": "str", "length": 9}
    assert len(publisher.sent) == 1


@pytest.mark.unit
def test_redacted_working_directory_event_id_is_stable_across_redelivery() -> None:
    """The content-addressed key must not drift when the shape is replayed."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionWorkEvents(publisher=_RecordingPublisher())
    for _ in range(3):
        handler.handle(
            {
                **json.loads(_LIVE_SESSION_STARTED),
                "_db": db,
                "_topic": TOPIC_SESSION_STARTED,
            }
        )
    assert len(db.query(TABLE)) == 1


@pytest.mark.unit
def test_working_directory_shape_never_leaks_into_summary_or_payload() -> None:
    """AC2. A shape renders as a length-only label and carries no directory text."""
    handler = HandlerProjectionWorkEvents(publisher=_RecordingPublisher())
    shaped = ModelWorkEventInbound(
        session_id="s-1",
        emitted_at="2026-09-25T00:15:09+00:00",
        working_directory={"type": "str", "length": len(_SENTINEL_DIRECTORY)},
    )
    row = handler.accumulate(shaped, TOPIC_SESSION_STARTED)
    assert row.summary == "session started in a redacted directory (9 chars)"
    assert _SENTINEL_DIRECTORY not in row.summary
    assert _SENTINEL_DIRECTORY not in json.dumps(row.payload)


@pytest.mark.unit
def test_working_directory_shape_never_leaks_plain_string_control() -> None:
    """AC2 control. An unredacted string still renders exactly as before."""
    handler = HandlerProjectionWorkEvents(publisher=_RecordingPublisher())
    plain = ModelWorkEventInbound(
        session_id="s-1",
        emitted_at="2026-09-25T00:15:09+00:00",
        working_directory=_SENTINEL_DIRECTORY,
    )
    row = handler.accumulate(plain, TOPIC_SESSION_STARTED)
    assert row.summary == f"session started in {_SENTINEL_DIRECTORY}"
    assert row.payload["working_directory"] == _SENTINEL_DIRECTORY


@pytest.mark.unit
def test_working_directory_shape_rejects_a_smuggled_content_key() -> None:
    """The shape is closed: a dict carrying anything but type/length is refused.

    Accepting an arbitrary dict would let a producer put the directory name back
    on the wire inside an object and have the ledger store it.
    """
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelWorkEventInbound(
            session_id="s-1",
            emitted_at="2026-09-25T00:15:09+00:00",
            working_directory={
                "type": "str",
                "length": 9,
                "value": _SENTINEL_DIRECTORY,
            },
        )
