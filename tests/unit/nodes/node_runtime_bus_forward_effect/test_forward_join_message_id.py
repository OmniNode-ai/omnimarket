# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The S1 join across the runtime-bus forwarder seam (OMN-24003): recorded event in, hosted-bus publish out.

``omnibase_core``'s ``RuntimeLocal`` hands the registered forwarder the events its in-memory bus recorded —
objects reading ``topic``, ``key_b64``, ``value_b64``, ``headers`` — ``CoreRuntimeBusForwarder.forward`` carries
them to the hosted bus through the injected publisher, and the receiving side of the seam reads back the same
bytes plus one identity: the ``message-id`` header its dedupe looks up. The join is judged in memory on both
sides at once: a ``RecordingPublisher`` (a ``ProtocolRuntimeBusPublisher``) stores every ``(topic, key, value,
headers)`` tuple handed over, and the receiving side's read (``message_id_of``) is run against exactly what the
forwarder put on the wire.

``ModelRuntimeBusEvent`` is not importable from the installed ``omnibase_core`` — this repo imports it nowhere,
the adapter reads the recorded events by attribute precisely because ``a core that predates them is no
obstacle``, and the entry-point seam test in ``test_runtime_bus_forward.py`` holds the join against core's own
settle step for the core pin (OMN-24000) — so the recorded event is stood up as a small frozen dataclass with
the four attribute names the seam reads. When the core pin lands the model, this stand-in should become it and
the shape guard below will then bite on a core-side rename. Every other test would follow unchanged.

The failing control: the forwarder is today a carrier — it forwards a record with no ``message-id`` header at
all, and the red belongs to the receiving side's identity read, which raises exactly when the header was
dropped.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_runtime_bus_forward_effect import (
    adapter_core_forwarder as adapter,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import (
    ModelRuntimeBusForwardOverlay,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.protocols import (
    ProtocolRuntimeBusPublisher,
    PublisherRefusedError,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_runtime_bus_forward_effect/overlay.example.yaml"
)
REFUSED_MESSAGE = "the hosted bus refused the publish"
READING = "example.evt.omnimarket.reading-taken.v1"
UNTRUSTED = "untrusted.topic.reading.v1"
KEY = "example-tenant-a"
VALUE = '{"kind": "example", "tenant": "example-tenant-a", "reading": 17}'
KEY_BYTES = KEY.encode("utf-8")
VALUE_BYTES = VALUE.encode("utf-8")
MESSAGE_ID = "5f0c6c1e-8d7a-4c3b-9e2f-1a2b3c4d5e6f"
SEAM_FIELDS = {"topic", "key_b64", "value_b64", "headers"}


@dataclasses.dataclass(frozen=True)
class RecordedRuntimeBusEvent:
    """One event a runtime-local bus recorded, shaped as the forwarder's seam reads it.

    The stand-in for omnibase_core's ``ModelRuntimeBusEvent`` — see the module docstring for why the core
    model itself is not importable yet and when this class should be replaced by it.
    """

    topic: str
    key_b64: str | None = None
    value_b64: str = ""
    headers: dict[str, str] = dataclasses.field(default_factory=dict)


class RecordingPublisher:
    """The recorded fake of the publisher seam: it stores what the hosted bus took.

    Implements ``ProtocolRuntimeBusPublisher``. ``published`` holds every publish in order as
    ``(topic, key, value, headers)`` tuples; a topic named in ``refuse_topics`` is answered with
    ``PublisherRefusedError`` instead and never recorded. The flush confirms everything.
    """

    def __init__(self, *, refuse_topics: tuple[str, ...] = ()) -> None:
        self.published: list[tuple[str, bytes | None, bytes, dict[str, str]]] = []
        self._refuse_topics = refuse_topics

    def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: dict[str, str]
    ) -> None:
        if topic in self._refuse_topics:
            raise PublisherRefusedError(f"{REFUSED_MESSAGE}: {topic}")
        self.published.append((topic, key, value, headers))

    def flush(self, timeout_s: float) -> int:
        return 0

    def failure_detail(self) -> str | None:
        return None


def message_id_of(published_headers: Mapping[str, str]) -> str:
    """The receiving side's identity read: the ``message-id`` header, its dedupe's lookup.

    Raises ``KeyError`` when the record carries no ``message-id`` — the shape a dedupe cannot accept.
    """
    return published_headers["message-id"]


def _overlay() -> ModelRuntimeBusForwardOverlay:
    """The example overlay's document: the forward's settings, none of them in this test."""
    document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    return ModelRuntimeBusForwardOverlay.model_validate(document)


def _recorded(
    topic: str,
    value: str,
    *,
    key: str | None = None,
    headers: dict[str, str] | None = None,
) -> RecordedRuntimeBusEvent:
    """One recorded event, encoded the way the runtime's recorder hands it over."""
    return RecordedRuntimeBusEvent(
        topic=topic,
        key_b64=(
            None
            if key is None
            else base64.b64encode(key.encode("utf-8")).decode("ascii")
        ),
        value_b64=base64.b64encode(value.encode("utf-8")).decode("ascii"),
        headers=dict(headers or {}),
    )


def _forward(
    monkeypatch: pytest.MonkeyPatch,
    recording: RecordingPublisher,
    *events: RecordedRuntimeBusEvent,
) -> dict[str, object]:
    """The core hook end to end: the recorded events carried through the injected publisher."""
    monkeypatch.setattr(adapter, "broker_for_lane", lambda lane, _env: lane)
    forwarder = adapter.CoreRuntimeBusForwarder(
        overlay=_overlay(),
        publisher_for_broker=lambda _broker: recording,
        environ={},
    )
    return forwarder.forward(events)


# ---------------------------------------------------------------------------------------------- golden


def test_golden_a_recorded_event_is_carried_bytes_and_identity_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = RecordingPublisher()
    assert isinstance(recording, ProtocolRuntimeBusPublisher)
    receipt = _forward(
        monkeypatch,
        recording,
        _recorded(READING, VALUE, key=KEY, headers={"message-id": MESSAGE_ID}),
    )

    [publish] = recording.published
    topic, key, value, headers = publish
    assert topic == READING
    assert key == KEY_BYTES
    assert value == VALUE_BYTES  # bytes on the wire, compared as bytes, never parsed
    assert message_id_of(headers) == MESSAGE_ID
    assert receipt == {"forwarded": 1, "failed": 0, "detail": None}


def test_golden_a_second_source_like_header_rides_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forwarder is today a carrier: the exact header dict recorded is the one handed over."""
    recorded_headers = {"message-id": MESSAGE_ID, "source": "example-runtime-a"}
    recording = RecordingPublisher()

    receipt = _forward(
        monkeypatch, recording, _recorded(READING, VALUE, headers=recorded_headers)
    )

    [publish] = recording.published
    assert publish[3] == {"message-id": MESSAGE_ID, "source": "example-runtime-a"}
    assert receipt["forwarded"] == 1


# ---------------------------------------------------------------------------------------------- error


def test_error_a_publish_the_client_refuses_counts_failed_and_carries_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = RecordingPublisher(refuse_topics=(READING,))

    receipt = _forward(
        monkeypatch,
        recording,
        _recorded(READING, VALUE, key=KEY, headers={"message-id": MESSAGE_ID}),
    )

    assert recording.published == []
    assert receipt["forwarded"] == 0
    assert receipt["failed"] == 1
    assert receipt["detail"] is not None
    assert REFUSED_MESSAGE in str(receipt["detail"])
    assert READING in str(receipt["detail"])


def test_error_a_topic_outside_the_allowed_prefixes_is_refused_by_name_and_never_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = RecordingPublisher()

    receipt = _forward(
        monkeypatch,
        recording,
        _recorded(UNTRUSTED, VALUE, headers={"message-id": MESSAGE_ID}),
    )

    assert recording.published == []
    assert receipt["forwarded"] == 0
    assert receipt["failed"] == 1
    assert receipt["detail"] is not None
    assert "topic outside allowed_topic_prefixes" in str(receipt["detail"])


# --------------------------------------------------------------------------------------------- controls


def test_control_the_identity_read_returns_the_recorded_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = RecordingPublisher()

    _forward(
        monkeypatch,
        recording,
        _recorded(READING, VALUE, headers={"message-id": MESSAGE_ID}),
    )

    [publish] = recording.published
    assert message_id_of(publish[3]) == MESSAGE_ID


def test_control_the_identity_read_raises_when_the_message_id_header_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The red is the dropped header's: the carrier forwards the record regardless; the dedupe's
    identity read is what fails, on the headers exactly as they were published."""
    recording = RecordingPublisher()

    receipt = _forward(
        monkeypatch,
        recording,
        _recorded(READING, VALUE, headers={"source": "example-runtime-a"}),
    )

    assert receipt["forwarded"] == 1
    [publish] = recording.published
    with pytest.raises(KeyError):
        message_id_of(publish[3])


# ----------------------------------------------------------------------------------------------- guard


def _forwarder_read_names() -> set[str]:
    """The field names the forwarder reads off a recorded event: its seam protocol's properties."""
    return {
        name
        for name, member in vars(adapter.ProtocolRecordedEvent).items()
        if isinstance(member, property)
    }


def test_guard_the_recorded_event_type_carries_every_field_the_forwarder_reads() -> (
    None
):
    """A rename on either side of the seam turns this red: the four fields the forwarder reads must
    stay the ones it declares, and the recorded event type must still carry every one of them."""
    read_names = _forwarder_read_names()
    event_fields = {field.name for field in dataclasses.fields(RecordedRuntimeBusEvent)}
    assert read_names >= SEAM_FIELDS, "the forwarder's own seam contract moved"
    assert read_names <= event_fields, (
        "the recorded event type no longer carries every field the forwarder reads"
    )
