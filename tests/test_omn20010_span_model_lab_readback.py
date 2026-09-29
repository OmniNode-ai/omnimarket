# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20010: a subagent span row carries the sidecar model, read back on a lab slot.

Runs on a lab pool host, where the pool driver's tests phase executes it beside
the slot's compose project. It publishes one hook event whose lineage names a
model and a description onto the slot's own bus, through the slot runtime
container's broker credentials, and reads ``claude_agent_spans`` back through
that container's database DSN. Both halves run inside the container, so no
credential leaves it and neither the compose port map nor the slot's database
suffix is assumed here.

It SKIPS everywhere else (no docker, no slot runtime container), which is the
same posture as ``test_omn19513_claude_hook_events_real_postgres.py``: the lab
readback is the pool run's evidence, not a CI job.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_capture"
_SLOT_RUNTIME = re.compile(
    r"^(omninode-prepr-\d-runtime|omnibase-infra-local-omninode-runtime)$"
)
_MODEL = "claude-sonnet-5-5"
_DESCRIPTION = "OMN-20010 lab readback"

# Runs INSIDE the slot runtime container: reads one event from stdin, publishes
# it onto the slot's namespaced topic, then polls the span row until it appears.
_IN_CONTAINER = r"""
import asyncio, json, os, sys
import asyncpg
from aiokafka import AIOKafkaProducer

spec = json.load(sys.stdin)
namespace = os.environ.get("KAFKA_TOPIC_NAMESPACE", "").strip()
topic = f"{namespace}.{spec['topic']}" if namespace else spec["topic"]


async def main() -> int:
    kwargs = {}
    if os.environ.get("KAFKA_SECURITY_PROTOCOL"):
        kwargs["security_protocol"] = os.environ["KAFKA_SECURITY_PROTOCOL"]
    if os.environ.get("KAFKA_SASL_MECHANISM"):
        kwargs["sasl_mechanism"] = os.environ["KAFKA_SASL_MECHANISM"]
        kwargs["sasl_plain_username"] = os.environ["KAFKA_SASL_USERNAME"]
        kwargs["sasl_plain_password"] = os.environ["KAFKA_SASL_PASSWORD"]
    producer = AIOKafkaProducer(
        bootstrap_servers=os.environ["KAFKA_BOOTSTRAP_SERVERS"], **kwargs
    )
    await producer.start()
    try:
        await producer.send_and_wait(
            topic,
            json.dumps(spec["event"]).encode(),
            key=spec["event"]["lineage"]["session_id"].encode(),
        )
    finally:
        await producer.stop()
    conn = await asyncpg.connect(os.environ["OMNINODE_INTERNAL_DB_URL"])
    try:
        for _ in range(spec["polls"]):
            row = await conn.fetchrow(
                "SELECT model, description FROM omninode_internal.claude_agent_spans "
                "WHERE session_id = $1 AND agent_id = $2",
                spec["event"]["lineage"]["session_id"],
                spec["event"]["lineage"]["agent_id"],
            )
            if row is not None:
                print(json.dumps({"topic": topic, "row": dict(row)}))
                return 0
            await asyncio.sleep(2)
    finally:
        await conn.close()
    print(json.dumps({"topic": topic, "row": None}))
    return 1


sys.exit(asyncio.run(main()))
"""


def _slot_runtime_container() -> str:
    if shutil.which("docker") is None:
        pytest.skip("no docker on this host: the lab readback runs on a pool host")
    listing = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    names = [n for n in listing.stdout.split() if _SLOT_RUNTIME.match(n)]
    if not names:
        pytest.skip("no pool slot runtime container is running on this host")
    return sorted(names)[0]


def _subagent_start_event() -> dict[str, Any]:
    scenario = _FIXTURES / "scenarios" / "subagent_tree.events.jsonl"
    event: dict[str, Any] = json.loads(scenario.read_text("utf-8").splitlines()[2])
    assert event["hook_event_name"] == "SubagentStart"
    suffix = uuid.uuid4().hex[:12]
    event["event_id"] = str(uuid.uuid4())
    event["lineage"]["session_id"] = f"lab-readback-{suffix}"
    event["lineage"]["agent_id"] = f"agent-lab-{suffix}"
    event["lineage"]["agent_model"] = _MODEL
    event["lineage"]["agent_description"] = _DESCRIPTION
    event["payload"]["agent_id"] = event["lineage"]["agent_id"]
    return event


def test_a_subagent_span_row_carries_the_sidecar_model_on_the_slot() -> None:
    container = _slot_runtime_container()
    spec = {
        "topic": "onex.evt.omniclaude.hook-event.v1",  # onex-topic-allow: the capture contract's metadata topic
        "event": _subagent_start_event(),
        "polls": 45,
    }
    run = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-c", _IN_CONTAINER],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert run.stdout.strip(), f"no readback from {container}: {run.stderr[-800:]}"
    readback = json.loads(run.stdout.strip().splitlines()[-1])
    assert readback["row"] is not None, (
        f"no span row for the published event on {readback['topic']} "
        f"(container {container})"
    )
    assert readback["row"]["model"] == _MODEL
    assert readback["row"]["description"] == _DESCRIPTION
