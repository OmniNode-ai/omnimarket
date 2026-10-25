# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21334: one compaction against a live pre-PR runtime slot returns a built package.

The probe runs inside the slot's effects runtime and publishes the compaction
command on the slot's own topic namespace (``KAFKA_TOPIC_NAMESPACE``): ``onex
run-node`` publishes on the bare topic names, so on a namespaced slot it reaches
the shared dev lane instead of the slot's handler. It awaits the package reply
and the fallback event. It needs a live Kafka broker, so it carries the ``kafka``
marker and hosted CI's ``-m "not kafka"`` selection never collects it; it also
skips when no pre-PR slot is up on the host. The lab pool's
``prepr_runtime_pool.py verify --tests`` runs it on a slot built from the ref.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from omnimarket.events.topics import (
    CONTEXT_COMPACTION_FALLBACK_TOPIC_V1,
    CONTEXT_COMPACTION_PACKAGE_TOPIC_V1,
    CONTEXT_COMPACTION_REQUESTED_TOPIC_V1,
)

pytestmark = [pytest.mark.integration, pytest.mark.kafka]

_SLOT_MARK = "prepr"
_RUN_TIMEOUT_S = 120
_LOG_TAIL_LINES = 400
_PUBLISH_AND_AWAIT = """
import asyncio, hashlib, json, os, sys, uuid
from datetime import UTC, datetime
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env
ns = os.environ["KAFKA_TOPIC_NAMESPACE"]
boot = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or (
    os.environ["KAFKA_BROKER_ALLOWLIST"].rstrip(":") + ":9092")
auth = build_aiokafka_auth_kwargs_from_env()
request, package, fallback = sys.argv[1:4]
async def main():
    invocation_id = str(uuid.uuid4())
    command = {
        "invocation_id": invocation_id,
        "session_id": "omn21334-slot-probe",
        "cwd": "/probe",
        "trigger": "manual",
        "input_messages_hash": hashlib.sha256(b"omn21334-probe").hexdigest(),
        "barrier_id": str(uuid.uuid4()),
    }
    envelope = {
        "payload": command,
        "envelope_id": str(uuid.uuid4()),
        "envelope_timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": str(uuid.uuid4()),
        "target_tool": "node_context_compaction_orchestrator",
        "priority": 5,
        "retry_count": 0,
        "envelope_version": {"major": 2, "minor": 3, "patch": 0},
        "onex_version": {"major": 1, "minor": 0, "patch": 0},
    }
    consumer = AIOKafkaConsumer(
        f"{ns}.{package}", f"{ns}.{fallback}", bootstrap_servers=boot,
        auto_offset_reset="latest", group_id=None, **auth)
    await consumer.start()
    await asyncio.sleep(4)
    producer = AIOKafkaProducer(bootstrap_servers=boot, **auth)
    await producer.start()
    await producer.send_and_wait(f"{ns}.{request}", json.dumps(envelope).encode())
    await producer.stop()
    seen = {}
    loop = asyncio.get_running_loop()
    end = loop.time() + 90
    while loop.time() < end and package not in seen:
        for tp, msgs in (await consumer.getmany(timeout_ms=3000)).items():
            for message in msgs:
                body = json.loads(message.value)["payload"]
                if body.get("invocation_id") == invocation_id:
                    seen[tp.topic.removeprefix(ns + ".")] = body
    await consumer.stop()
    print(json.dumps({"invocation_id": invocation_id, "seen": seen}))
asyncio.run(main())
"""


def _docker(
    *args: str, timeout: int = 30, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _slot_effects_container() -> str | None:
    """The running effects runtime of a pre-PR slot, found by its compose project."""
    if shutil.which("docker") is None:
        return None
    listed = _docker(
        "ps", "--format", '{{.Names}}\t{{.Label "com.docker.compose.project"}}'
    )
    for row in listed.stdout.splitlines():
        name, _, project = row.partition("\t")
        if "runtime-effects" in name and _SLOT_MARK in project:
            return name
    return None


def _compaction_log_lines(container: str) -> list[str]:
    logs = _docker("logs", "--tail", str(_LOG_TAIL_LINES), container)
    return [
        line[:400]
        for line in (logs.stdout + logs.stderr).splitlines()
        if "fell back to native" in line
    ][-5:]


def test_compaction_on_a_slot_returns_a_built_package() -> None:
    container = _slot_effects_container()
    if container is None:
        pytest.skip("no pre-PR runtime slot is up on this host")
    ran = _docker(
        "exec",
        "-i",
        container,
        "/app/.venv/bin/python",
        "-",
        CONTEXT_COMPACTION_REQUESTED_TOPIC_V1,
        CONTEXT_COMPACTION_PACKAGE_TOPIC_V1,
        CONTEXT_COMPACTION_FALLBACK_TOPIC_V1,
        timeout=_RUN_TIMEOUT_S,
        stdin=_PUBLISH_AND_AWAIT,
    )
    evidence = {
        "container": container,
        "exit": ran.returncode,
        "stdout": ran.stdout[-2500:],
        "stderr": ran.stderr[-800:],
        "runtime_log": _compaction_log_lines(container),
    }
    print("COMPACTION-PROBE " + json.dumps(evidence, sort_keys=True))
    assert ran.returncode == 0, f"probe failed: {json.dumps(evidence)}"
    seen = json.loads(ran.stdout.strip().splitlines()[-1])["seen"]
    reply = seen.get(CONTEXT_COMPACTION_PACKAGE_TOPIC_V1, {})
    assert reply.get("status") == "built", f"native fallback: {json.dumps(evidence)}"
    assert reply.get("package_id"), f"built reply carries no package id: {reply}"
