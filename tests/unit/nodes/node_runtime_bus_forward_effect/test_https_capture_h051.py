# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One capture/compare leg usable against a loopback door on the lab."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnimarket.nodes.node_runtime_bus_forward_effect.handlers import (
    HandlerRuntimeBusForward,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.models import (
    ModelRuntimeBusForwardCommand,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.protocols import (
    ProtocolRuntimeBusPublisher,
)

from .https_test_door import TOPIC, RecordingDoor, loopback_door, overlay, records
from .test_publisher_https import b64, event, publisher


class CapturePublisher:
    """Write the bytes handed to the publisher and pass through the same call."""

    def __init__(
        self, delegate: ProtocolRuntimeBusPublisher, evidence: Path, run_id: str
    ) -> None:
        self._delegate = delegate
        self._evidence = evidence
        self._run_id = run_id

    def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: dict[str, str]
    ) -> None:
        from omnibase_core.models.contracts.model_ingest_contract import (
            compute_event_id,
        )

        event_id = compute_event_id(
            topic,
            key,
            value,
            {name: text.encode("utf-8") for name, text in headers.items()},
        )
        with self._evidence.open("a", encoding="utf-8") as capture:
            capture.write(
                json.dumps(
                    {
                        "run_id": self._run_id,
                        "topic": topic,
                        "key": None if key is None else b64(key),
                        "value": b64(value),
                        "headers": headers,
                        "event_id": event_id,
                    }
                )
                + "\n"
            )
        self._delegate.publish(topic, key, value, headers)

    def flush(self, timeout_s: float) -> int:
        return self._delegate.flush(timeout_s)

    def failure_detail(self) -> str | None:
        return self._delegate.failure_detail()


@pytest.mark.parametrize(
    "real_socket",
    [False, pytest.param(True, marks=pytest.mark.integration)],
    ids=["mock_transport", "tls_loopback"],
)
# - the h051 capture leg (review round 4): a recording wrapper around `PublisherHttps` writes, per
# `publish`, the exact key, value bytes and headers handed to it and the `event_id` that
# `compute_event_id` returns to an evidence file, then passes the call through unchanged; a test
# with a `MockTransport` door checks the file holds the bytes the door received and an `event_id`
# equal to the door's recompute -- falsifier: `pytest ... -k https_capture` with the wrapper
# recording a re-encoded copy of the value turns the bytes case red
def test_https_capture_keeps_original_bytes_and_matches_the_door(
    tmp_path: Path, real_socket: bool
) -> None:
    from omnibase_core.models.contracts.model_ingest_contract import compute_event_id

    configuration = overlay()
    evidence = tmp_path / "https-capture.ndjson"
    run_id = "capture-run"
    key = b"\x00\xff"
    value = b'{ "run_id" : "capture-run", "value": "\xc3\xa9" }\n'
    headers = {"message-id": "capture-id", "trace": "original"}
    command = ModelRuntimeBusForwardCommand(
        run_id=run_id, events=(event(value, key=key, headers=headers),)
    )
    if real_socket:
        with loopback_door(tmp_path) as (url, verify, observed):
            sender = publisher(
                configuration,
                url=url,
                transport_factory=lambda: httpx.AsyncHTTPTransport(verify=verify),
            )
            handler = HandlerRuntimeBusForward(
                configuration, CapturePublisher(sender, evidence, run_id)
            )
            result = handler.handle(command)
            assert len(observed) == 1
            wire = records(observed[0][1])
    else:
        door = RecordingDoor()
        sender = publisher(configuration, door=door)
        handler = HandlerRuntimeBusForward(
            configuration, CapturePublisher(sender, evidence, run_id)
        )
        result = handler.handle(command)
        assert len(door.requests) == 1
        wire = records(door.requests[0].content)

    assert result.receipt() == {"forwarded": 1, "failed": 0, "detail": None}
    expected_id = compute_event_id(
        TOPIC, key, value, {k: v.encode("utf-8") for k, v in headers.items()}
    )
    assert wire == [
        {
            "topic": TOPIC,
            "key": b64(key),
            "value": b64(value),
            "headers": headers,
            "event_id": expected_id,
        }
    ]
    assert [
        json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines()
    ] == [{"run_id": run_id, **wire[0]}]
