# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""End-to-end emit daemon health probe."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from omnimarket.events.daemon_health_probe import (
    ModelDaemonHealthProbeResult,
)
from omnimarket.nodes.node_emit_daemon.client import EmitClient, default_socket_path
from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry

HEALTH_PROBE_EVENT_TYPE = "daemon.health.probe"
_DEFAULT_TIMEOUT_S = 5.0
_DEFAULT_POLL_INTERVAL_S = 0.1


class ProtocolProbeClient(Protocol):
    def emit_sync(self, event_type: str, payload: dict[str, object]) -> str: ...

    def close(self) -> None: ...


class ProtocolProbeConsumer(Protocol):
    def subscribe(self, topics: list[str]) -> None: ...

    def poll(self, timeout: float) -> object | None: ...

    def close(self) -> None: ...


ProbeClientFactory = Callable[[str, float], ProtocolProbeClient]
ProbeConsumerFactory = Callable[[str, str], ProtocolProbeConsumer]


def _default_registry_path() -> Path:
    return Path(__file__).resolve().parent / "registries" / "topics.yaml"


def _load_probe_topic(event_registry_path: Path | None) -> str:
    registry = EventRegistry.from_yaml(event_registry_path or _default_registry_path())
    registration = registry.get_registration(HEALTH_PROBE_EVENT_TYPE)
    if registration is None:
        raise ValueError(f"{HEALTH_PROBE_EVENT_TYPE} is not registered")
    if not registration.fan_out:
        raise ValueError(f"{HEALTH_PROBE_EVENT_TYPE} has no fan-out topic")
    return registration.fan_out[0].topic


def _default_client_factory(socket_path: str, timeout_s: float) -> ProtocolProbeClient:
    return EmitClient(socket_path=socket_path, timeout=timeout_s)


def _default_consumer_factory(
    bootstrap_servers: str, group_id: str
) -> ProtocolProbeConsumer:
    try:
        from confluent_kafka import Consumer
    except ImportError as exc:  # pragma: no cover - depends on host extras
        raise RuntimeError(
            "confluent_kafka is required for live health probes"
        ) from exc

    return cast(
        ProtocolProbeConsumer,
        Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "auto.offset.reset": "latest",
                "enable.auto.commit": False,
            }
        ),
    )


def _record_value(record: object) -> object:
    if isinstance(record, dict):
        return record.get("value")
    value_attr = getattr(record, "value", None)
    if callable(value_attr):
        return value_attr()
    return value_attr


def _record_offset(record: object) -> int | None:
    if isinstance(record, dict):
        raw_offset = record.get("offset")
    else:
        offset_attr = getattr(record, "offset", None)
        raw_offset = offset_attr() if callable(offset_attr) else offset_attr
    if isinstance(raw_offset, int):
        return raw_offset
    return None


def _record_error(record: object) -> object | None:
    if isinstance(record, dict):
        return record.get("error")
    error_attr = getattr(record, "error", None)
    return error_attr() if callable(error_attr) else error_attr


def _decode_record_payload(record: object) -> dict[str, object] | None:
    raw_value = _record_value(record)
    if isinstance(raw_value, bytes):
        try:
            decoded = raw_value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(raw_value, str):
        decoded = raw_value
    elif isinstance(raw_value, dict):
        return raw_value
    else:
        return None

    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _result(
    *,
    success: bool,
    correlation_id: str,
    reason: str,
    socket_path: str,
    bootstrap_servers: str,
    topic: str | None = None,
    event_id: str | None = None,
    kafka_offset: int | None = None,
    round_trip_ms: float | None = None,
) -> ModelDaemonHealthProbeResult:
    return ModelDaemonHealthProbeResult(
        success=success,
        correlation_id=correlation_id,
        reason=reason,
        socket_path=socket_path,
        bootstrap_servers=bootstrap_servers,
        topic=topic,
        event_id=event_id,
        kafka_offset=kafka_offset,
        round_trip_ms=round_trip_ms,
    )


def probe(
    socket_path: str,
    bootstrap_servers: str,
    correlation_id: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    *,
    event_registry_path: Path | None = None,
    client_factory: ProbeClientFactory = _default_client_factory,
    consumer_factory: ProbeConsumerFactory = _default_consumer_factory,
) -> ModelDaemonHealthProbeResult:
    """Run a socket-to-Kafka daemon health probe."""

    corr_id = correlation_id or str(uuid4())
    if timeout_s <= 0:
        raise ValueError("timeout_s must be greater than zero")

    if not Path(socket_path).exists():
        return _result(
            success=False,
            correlation_id=corr_id,
            reason=f"socket missing: {socket_path}",
            socket_path=socket_path,
            bootstrap_servers=bootstrap_servers,
        )

    try:
        topic = _load_probe_topic(event_registry_path)
    except Exception as exc:
        return _result(
            success=False,
            correlation_id=corr_id,
            reason=f"event registry unavailable: {exc}",
            socket_path=socket_path,
            bootstrap_servers=bootstrap_servers,
        )

    group_id = f"daemon-health-probe-{corr_id}"
    consumer: ProtocolProbeConsumer | None = None
    client: ProtocolProbeClient | None = None
    started = time.monotonic()
    try:
        consumer = consumer_factory(bootstrap_servers, group_id)
        consumer.subscribe([topic])
    except Exception as exc:
        if consumer is not None:
            consumer.close()
        return _result(
            success=False,
            correlation_id=corr_id,
            reason=f"kafka unreachable: {exc}",
            socket_path=socket_path,
            bootstrap_servers=bootstrap_servers,
            topic=topic,
        )

    try:
        payload: dict[str, object] = {
            "correlation_id": corr_id,
            "probe": "daemon-health",
            "sent_at_monotonic": started,
        }
        client = client_factory(socket_path, timeout_s)
        event_id = client.emit_sync(HEALTH_PROBE_EVENT_TYPE, payload)
    except Exception as exc:
        with contextlib.suppress(Exception):
            consumer.close()
        return _result(
            success=False,
            correlation_id=corr_id,
            reason=f"daemon unreachable: {exc}",
            socket_path=socket_path,
            bootstrap_servers=bootstrap_servers,
            topic=topic,
        )
    finally:
        if client is not None:
            client.close()

    deadline = started + timeout_s
    last_error: object | None = None
    try:
        while time.monotonic() < deadline:
            remaining = max(
                0.0, min(_DEFAULT_POLL_INTERVAL_S, deadline - time.monotonic())
            )
            record = consumer.poll(remaining)
            if record is None:
                continue
            last_error = _record_error(record)
            if last_error is not None:
                continue
            record_payload = _decode_record_payload(record)
            if record_payload is None:
                continue
            if record_payload.get("correlation_id") != corr_id:
                continue
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return _result(
                success=True,
                correlation_id=corr_id,
                reason="daemon health probe round trip succeeded",
                socket_path=socket_path,
                bootstrap_servers=bootstrap_servers,
                topic=topic,
                event_id=event_id,
                kafka_offset=_record_offset(record),
                round_trip_ms=elapsed_ms,
            )
    finally:
        consumer.close()

    reason = f"timed out waiting for correlated Kafka event: {corr_id}"
    if last_error is not None:
        reason = f"kafka consumer error while waiting for {corr_id}: {last_error}"
    return _result(
        success=False,
        correlation_id=corr_id,
        reason=reason,
        socket_path=socket_path,
        bootstrap_servers=bootstrap_servers,
        topic=topic,
        event_id=event_id,
        round_trip_ms=(time.monotonic() - started) * 1000.0,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-path", default=default_socket_path())
    parser.add_argument("--bootstrap-servers", required=True)
    parser.add_argument("--correlation-id")
    parser.add_argument("--timeout-s", type=float, default=_DEFAULT_TIMEOUT_S)
    parser.add_argument("--event-registry-path", type=Path, default=None)
    args = parser.parse_args(argv)

    result = probe(
        socket_path=args.socket_path,
        bootstrap_servers=args.bootstrap_servers,
        correlation_id=args.correlation_id,
        timeout_s=args.timeout_s,
        event_registry_path=args.event_registry_path,
    )
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__: list[str] = [
    "HEALTH_PROBE_EVENT_TYPE",
    "ProtocolProbeClient",
    "ProtocolProbeConsumer",
    "main",
    "probe",
]
