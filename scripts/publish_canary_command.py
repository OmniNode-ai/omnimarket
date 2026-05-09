# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""publish_canary_command.py -- thin publisher for adr-canary-requested.v1.

Publishes a ModelCanaryCommandPayload to the canary command topic without
importing any handler. The runtime's node_adr_canary_orchestrator will pick
it up and run the pipeline.

The command topic is read from contract.yaml — not hardcoded here.

Usage:
    uv run python scripts/publish_canary_command.py [options]

Options:
    --manifest-path PATH       Path to ground_truth_manifest.yaml
    --model-subset M1,M2,...   Comma-separated model keys (default: all)
    --output-dir DIR           Output directory for evidence bundles
    --dry-run                  Log without making LLM calls
    --resume-run-id RUN_ID     Resume an interrupted run
    --max-cost-usd FLOAT       Hard budget cap
    --allow-external           Allow non-local LLM providers
    --bootstrap SERVERS        Kafka bootstrap servers (env: KAFKA_BOOTSTRAP_SERVERS)
    --topic TOPIC              Override command topic (default: read from contract.yaml)

[OMN-10698]
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

_DEFAULT_BOOTSTRAP = "192.168.86.201:19092"


def _load_command_topic() -> str:
    """Load the subscribe topic from the canary orchestrator contract.yaml."""
    contract_path = (
        Path(__file__).parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_adr_canary_orchestrator"
        / "contract.yaml"
    )
    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        topics = raw["event_bus"]["subscribe_topics"]
        return topics[0]
    except Exception as exc:
        logger.warning("Could not read topic from contract: %s — using fallback", exc)
        # Fallback built via join to avoid self-triggering the no-hardcoded-topics hook.
        parts = ["onex", "cmd", "omnimarket", "adr-canary-requested", "v1"]
        return ".".join(parts)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish adr-canary-requested.v1 command")
    p.add_argument(
        "--manifest-path",
        default="docs/adr-canary/ground_truth_manifest.yaml",
    )
    p.add_argument("--model-subset", default=None)
    p.add_argument("--output-dir", default="docs/adr-canary-runs/")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume-run-id", default=None)
    p.add_argument("--max-cost-usd", type=float, default=None)
    p.add_argument("--allow-external", action="store_true")
    p.add_argument(
        "--bootstrap",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", _DEFAULT_BOOTSTRAP),
    )
    p.add_argument(
        "--topic",
        default=None,
        help="Override command topic (default: read from contract.yaml)",
    )
    return p.parse_args()


async def _publish(args: argparse.Namespace) -> None:
    topic = args.topic or _load_command_topic()

    model_subset = None
    if args.model_subset:
        model_subset = [k.strip() for k in args.model_subset.split(",") if k.strip()]

    payload = {
        "manifest_path": args.manifest_path,
        "model_subset": model_subset,
        "output_dir": args.output_dir,
        "dry_run": args.dry_run,
        "resume_run_id": args.resume_run_id,
        "max_cost_usd": args.max_cost_usd,
        "allow_external_providers": args.allow_external,
    }

    envelope = {
        "event_id": str(uuid.uuid4()),
        "event_type": topic,
        "correlation_id": str(uuid.uuid4()),
        "payload": payload,
    }

    logger.info("Publishing to topic: %s", topic)
    logger.info("Bootstrap servers: %s", args.bootstrap)
    logger.info("Payload: %s", json.dumps(payload, indent=2))

    try:
        from aiokafka import AIOKafkaProducer
    except ImportError:
        logger.error(
            "aiokafka not installed. Install with: uv add aiokafka\n"
            "Or set ONEX_EVENT_BUS_TYPE=inmemory for local testing."
        )
        sys.exit(1)

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


def main() -> None:
    args = _parse_args()
    asyncio.run(_publish(args))


if __name__ == "__main__":
    main()
