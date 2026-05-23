# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Thin command publisher for KB ADR publishing.

Reads the subscribe topic from node_kb_adr_publisher/contract.yaml and
publishes a command envelope to the bus via aiokafka.

This script ONLY publishes a command event to the bus.
It does NOT import any handlers or adapters.

Usage:
    uv run python scripts/publish_kb_adr_command.py \\
        --canary-run-dir docs/adr-canary-runs/<run_id>/ \\
        --model-key qwen3-coder-local \\
        [--dry-run]

[OMN-11808]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _load_command_topic() -> str:
    """Load the subscribe topic from the node contract — never hardcode."""
    contract_path = (
        Path(__file__).parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_kb_adr_publisher"
        / "contract.yaml"
    )
    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        topics = raw["event_bus"]["subscribe_topics"]
        return topics[0]
    except Exception as exc:
        logger.warning("Could not read topic from contract: %s — using fallback", exc)
        parts = ["onex", "cmd", "omnimarket", "kb-adr-publish-requested", "v1"]
        return ".".join(parts)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Publish kb-adr-publish-requested.v1 command to bus"
    )
    p.add_argument(
        "--canary-run-dir", required=True, help="Path to canary run output directory"
    )
    p.add_argument("--model-key", required=True, help="Model key to filter extractions")
    p.add_argument(
        "--kb-repo", default="OmniNode-ai/knowledge-base", help="Target KB repo slug"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Pass dry_run=true in payload"
    )
    p.add_argument(
        "--bootstrap",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", ""),
        help="Kafka bootstrap servers (env: KAFKA_BOOTSTRAP_SERVERS)",
    )
    p.add_argument("--topic", default=None, help="Override command topic")
    return p.parse_args()


async def _publish(args: argparse.Namespace) -> int:
    topic = args.topic or _load_command_topic()

    if not args.bootstrap:
        logger.error("KAFKA_BOOTSTRAP_SERVERS not set and --bootstrap not provided")
        return 1

    payload = {
        "canary_run_dir": args.canary_run_dir,
        "model_key": args.model_key,
        "kb_repo": args.kb_repo,
        "dry_run": args.dry_run,
    }

    envelope = {
        "event_id": str(uuid.uuid4()),
        "event_type": topic,
        "correlation_id": str(uuid.uuid4()),
        "payload": payload,
    }

    logger.info("Publishing to topic: %s", topic)
    logger.info("Payload: %s", json.dumps(payload, indent=2))

    try:
        from aiokafka import AIOKafkaProducer
    except ImportError:
        logger.error("aiokafka not installed. Install with: uv add aiokafka")
        return 1

    producer = AIOKafkaProducer(
        bootstrap_servers=args.bootstrap,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=envelope)
        logger.info("Published successfully (event_id=%s)", envelope["event_id"])
    finally:
        await producer.stop()

    return 0


def main() -> None:
    args = _parse_args()
    sys.exit(asyncio.run(_publish(args)))


if __name__ == "__main__":
    main()
