#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Thin publisher for the delegation-eval label-record command.

Operator entry point for node_delegation_eval_orchestrator: reads one label per
JSONL line, validates each as ModelLabelRecordRequest, and publishes the bare
request (no envelope) to the node's command topic. The topic is read from the
node's contract.yaml, never hardcoded. Nothing is published unless every line
validates.

Each line: {"correlation_id", "attempt_index", "label", "rater_role",
"rubric_version", "stratum", "computed_facts"}; tenant_id comes from --tenant-id.

Usage:
    uv run python -m omnimarket.nodes.node_delegation_eval_orchestrator.publish_label_record \
        --tenant-id <uuid> --labels labels.jsonl \
        (--bootstrap-servers <host:port> | --dry-run)

Kafka: --bootstrap-servers plus the SASL variables the shared auth helper reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import yaml
from pydantic import ValidationError

from omnimarket.events.delegation_eval import ModelLabelRecordRequest

_CONTRACT = Path(__file__).resolve().parent / "contract.yaml"


class ProtocolLabelProducer(Protocol):
    def send(self, topic: str, value: dict[str, object]) -> None: ...


def command_topic() -> str:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    topic = contract["runtime_dispatch"]["command_topic"]
    if topic not in contract["event_bus"]["subscribe_topics"]:
        raise SystemExit(f"{topic} is not a subscribe topic of {_CONTRACT}")
    return str(topic)


def build_requests(
    lines: Iterable[str], tenant_id: str
) -> list[ModelLabelRecordRequest]:
    try:
        tenant = str(UUID(tenant_id))
    except ValueError:
        raise SystemExit(f"--tenant-id is not a UUID: {tenant_id!r}") from None
    requests: list[ModelLabelRecordRequest] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            requests.append(
                ModelLabelRecordRequest.model_validate(
                    {**json.loads(line), "tenant_id": tenant}
                )
            )
        except (ValidationError, ValueError) as exc:
            raise SystemExit(f"label line {number} is invalid: {exc}") from None
    if not requests:
        raise SystemExit("no labels to publish")
    return requests


def publish_labels(
    lines: Iterable[str], tenant_id: str, producer: ProtocolLabelProducer
) -> int:
    requests = build_requests(lines, tenant_id)
    topic = command_topic()
    for request in requests:
        producer.send(topic, request.model_dump(mode="json"))
    return len(requests)


class _KafkaProducer:
    """Buffers sends, then flushes them in one async session."""

    def __init__(self, bootstrap: str) -> None:
        self._bootstrap = bootstrap
        self._pending: list[tuple[str, dict[str, object]]] = []

    def send(self, topic: str, value: dict[str, object]) -> None:
        self._pending.append((topic, value))

    async def flush(self) -> None:
        from aiokafka import AIOKafkaProducer
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            **build_aiokafka_auth_kwargs_from_env(),
        )
        await producer.start()
        try:
            for topic, value in self._pending:
                await producer.send_and_wait(topic, value=value)
        finally:
            await producer.stop()


class _DryRunProducer:
    def send(self, topic: str, value: dict[str, object]) -> None:
        sys.stdout.write(json.dumps({"topic": topic, "value": value}) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--labels", required=True, help="JSONL file, one label a line")
    parser.add_argument(
        "--bootstrap-servers", default="", help="Kafka bootstrap servers"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    lines = Path(args.labels).read_text(encoding="utf-8").splitlines()
    if args.dry_run:
        count = publish_labels(lines, args.tenant_id, _DryRunProducer())
    else:
        if not args.bootstrap_servers:
            parser.error("--bootstrap-servers is required unless --dry-run")
        kafka = _KafkaProducer(args.bootstrap_servers)
        count = publish_labels(lines, args.tenant_id, kafka)
        asyncio.run(kafka.flush())
    sys.stderr.write(f"published {count} label-record command(s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
