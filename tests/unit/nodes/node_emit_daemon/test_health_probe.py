"""Unit tests for emit daemon health probe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_emit_daemon.health_probe import (
    HEALTH_PROBE_EVENT_TYPE,
    main,
    probe,
)
from omnimarket.nodes.node_emit_daemon.models.model_daemon_health_probe_result import (
    ModelDaemonHealthProbeResult,
)


class _FakeClient:
    def __init__(self, events: list[tuple[str, dict[str, object]]]) -> None:
        self._events = events
        self.closed = False

    def emit_sync(self, event_type: str, payload: dict[str, object]) -> str:
        self._events.append((event_type, payload))
        return "evt-health-1"

    def close(self) -> None:
        self.closed = True


class _FakeConsumer:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records
        self.subscribed: list[str] = []
        self.closed = False

    def subscribe(self, topics: list[str]) -> None:
        self.subscribed = topics

    def poll(self, timeout: float) -> object | None:
        _ = timeout
        if not self._records:
            return None
        return self._records.pop(0)

    def close(self) -> None:
        self.closed = True


def _touch_socket_marker(tmp_path: Path) -> str:
    socket_path = tmp_path / "emit.sock"
    socket_path.touch()
    return str(socket_path)


def test_probe_returns_typed_failure_when_socket_missing(tmp_path: Path) -> None:
    result = probe(
        socket_path=str(tmp_path / "missing.sock"),
        bootstrap_servers="localhost:9092",
        correlation_id="corr-missing",
    )

    assert isinstance(result, ModelDaemonHealthProbeResult)
    assert result.success is False
    assert result.reason.startswith("socket missing:")
    assert result.correlation_id == "corr-missing"


def test_probe_returns_false_when_kafka_consumer_unavailable(tmp_path: Path) -> None:
    def failing_consumer_factory(
        bootstrap_servers: str, group_id: str
    ) -> _FakeConsumer:
        _ = bootstrap_servers, group_id
        raise RuntimeError("broker unavailable")

    result = probe(
        socket_path=_touch_socket_marker(tmp_path),
        bootstrap_servers="localhost:9092",
        correlation_id="corr-kafka-down",
        consumer_factory=failing_consumer_factory,
    )

    assert result.success is False
    assert result.reason == "kafka unreachable: broker unavailable"
    assert result.topic is not None


def test_probe_ignores_wrong_correlation_id_then_matches(tmp_path: Path) -> None:
    emitted: list[tuple[str, dict[str, object]]] = []
    records = [
        {
            "value": json.dumps({"correlation_id": "wrong-corr"}).encode(),
            "offset": 4,
        },
        {
            "value": json.dumps(
                {"correlation_id": "corr-match", "probe": "daemon-health"}
            ).encode(),
            "offset": 5,
        },
    ]

    result = probe(
        socket_path=_touch_socket_marker(tmp_path),
        bootstrap_servers="localhost:9092",
        correlation_id="corr-match",
        timeout_s=1,
        client_factory=lambda _socket_path, _timeout_s: _FakeClient(emitted),
        consumer_factory=lambda _bootstrap_servers, _group_id: _FakeConsumer(records),
    )

    assert result.success is True
    assert result.kafka_offset == 5
    assert result.event_id == "evt-health-1"
    assert emitted == [
        (
            HEALTH_PROBE_EVENT_TYPE,
            {
                "correlation_id": "corr-match",
                "probe": "daemon-health",
                "sent_at_monotonic": pytest.approx(emitted[0][1]["sent_at_monotonic"]),
            },
        )
    ]


def test_probe_closes_consumer_when_daemon_emit_fails(tmp_path: Path) -> None:
    consumer = _FakeConsumer([])

    def failing_client_factory(socket_path: str, timeout_s: float) -> _FakeClient:
        _ = socket_path, timeout_s
        raise RuntimeError("daemon refused connection")

    result = probe(
        socket_path=_touch_socket_marker(tmp_path),
        bootstrap_servers="localhost:9092",
        correlation_id="corr-daemon-down",
        client_factory=failing_client_factory,
        consumer_factory=lambda _bootstrap_servers, _group_id: consumer,
    )

    assert result.success is False
    assert result.reason == "daemon unreachable: daemon refused connection"
    assert consumer.closed is True


def test_probe_times_out_when_no_matching_correlation(tmp_path: Path) -> None:
    result = probe(
        socket_path=_touch_socket_marker(tmp_path),
        bootstrap_servers="localhost:9092",
        correlation_id="corr-timeout",
        timeout_s=0.01,
        client_factory=lambda _socket_path, _timeout_s: _FakeClient([]),
        consumer_factory=lambda _bootstrap_servers, _group_id: _FakeConsumer(
            [{"value": {"correlation_id": "other"}, "offset": 1}]
        ),
    )

    assert result.success is False
    assert result.reason == "timed out waiting for correlated Kafka event: corr-timeout"
    assert result.event_id == "evt-health-1"


def test_probe_requires_positive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        probe(
            socket_path=_touch_socket_marker(tmp_path),
            bootstrap_servers="localhost:9092",
            timeout_s=0,
        )


def test_cli_prints_json_result_for_missing_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "--socket-path",
            str(tmp_path / "missing.sock"),
            "--bootstrap-servers",
            "localhost:9092",
            "--correlation-id",
            "corr-cli",
        ]
    )

    assert rc == 1
    output = json.loads(capsys.readouterr().out)
    assert output["success"] is False
    assert output["correlation_id"] == "corr-cli"


def test_model_forbids_loose_fields() -> None:
    with pytest.raises(ValidationError, match="unexpected"):
        ModelDaemonHealthProbeResult.model_validate(
            {
                "success": False,
                "correlation_id": "corr",
                "reason": "x",
                "socket_path": "/tmp/sock",
                "bootstrap_servers": "localhost:9092",
                "unexpected": True,
            }
        )
