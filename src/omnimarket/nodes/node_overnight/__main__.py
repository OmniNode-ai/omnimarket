# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_overnight.

Usage:
    python -m omnimarket.nodes.node_overnight \
        --dry-run \
        --skip-build-loop

    python -m omnimarket.nodes.node_overnight \
        --contract-file path/to/overnight-contract.yaml \
        --dry-run

    python -m omnimarket.nodes.node_overnight \
        --contract-file path/to/overnight-contract.yaml \
        --dispatch-phases

    python -m omnimarket.nodes.node_overnight \
        --contract-file path/to/overnight-contract.yaml \
        --dispatch-phases \
        --dry-run

    python -m omnimarket.nodes.node_overnight \
        --contract-file path/to/overnight-contract.yaml \
        --dispatch-phases \
        --publish-events

Flags:
    --dispatch-phases / -d
        Invoke the real per-phase compute-node dispatcher for each non-skipped
        phase. Without this flag the CLI validates and sequences the phase list
        but treats every phase as vacuous-green (0 ms, 0 dispatches). Set this
        flag to perform actual overnight work (OMN-8402).

    --publish-events
        Construct a sync Kafka producer using KAFKA_BOOTSTRAP_SERVERS and inject
        it as the event_bus into HandlerOvernight. Phase-start, phase-end, and
        session-complete envelopes are published to the topics declared in
        contract.yaml (OMN-8403). Without this flag, event publishing is a no-op
        (event_bus=None default).

Outputs JSON to stdout: ModelOvernightResult model.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

import yaml
from onex_change_control.overseer.model_overnight_contract import ModelOvernightContract

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EventPublisher,
    HandlerOvernight,
    ModelOvernightCommand,
)

logger = logging.getLogger(__name__)


def _build_kafka_publisher() -> EventPublisher | None:
    """Construct a sync Kafka publisher from KAFKA_BOOTSTRAP_SERVERS.

    Uses confluent_kafka.Producer for synchronous fire-and-forget publishing.
    Returns None (no-op) when KAFKA_BOOTSTRAP_SERVERS is not set or when
    confluent_kafka is unavailable.

    The overnight session must never be taken down by a bus outage — callers
    should treat None as a signal to skip publishing, not as an error.
    """
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
    if not bootstrap:
        logger.warning(
            "[OVERNIGHT] KAFKA_BOOTSTRAP_SERVERS not set — event publishing disabled"
        )
        return None

    try:
        from confluent_kafka import Producer
    except ImportError:
        logger.warning(
            "[OVERNIGHT] confluent_kafka not available — event publishing disabled"
        )
        return None

    try:
        producer = Producer({"bootstrap.servers": bootstrap})
    except Exception as exc:
        logger.warning(
            "[OVERNIGHT] Kafka producer init failed: %s — event publishing disabled",
            exc,
        )
        return None

    def _publish(topic: str, payload: bytes) -> None:
        try:
            producer.produce(topic, value=payload)
            producer.poll(0)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[OVERNIGHT] Kafka produce to %s failed: %s", topic, exc)

    logger.info("[OVERNIGHT] Kafka event publisher ready (broker: %s)", bootstrap)
    return _publish


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Run the overnight autonomous pipeline."
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Maximum build loop cycles (0 = unlimited)",
    )
    parser.add_argument("--skip-build-loop", action="store_true", default=False)
    parser.add_argument("--skip-merge-sweep", action="store_true", default=False)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run all phases in dry-run mode",
    )
    parser.add_argument(
        "--contract-file",
        type=str,
        default=None,
        help=(
            "Path to a ModelOvernightContract YAML file. When set, the contract "
            "is loaded and attached to the overnight command, enabling cost "
            "ceiling and halt_on_failure enforcement."
        ),
    )
    parser.add_argument(
        "--dispatch-phases",
        action="store_true",
        default=False,
        help=(
            "When set, HandlerOvernight invokes the real per-phase dispatcher "
            "for each non-skipped phase instead of falling through as a "
            "vacuous-green sequencer. Without this flag the CLI only "
            "validates the contract and sequences the phase list."
        ),
    )
    parser.add_argument(
        "--publish-events",
        action="store_true",
        default=False,
        help=(
            "Publish phase-start, phase-end, and session-complete Kafka envelopes "
            "via KAFKA_BOOTSTRAP_SERVERS. Topics are declared in contract.yaml. "
            "When not set, event publishing is a no-op (OMN-8403)."
        ),
    )

    args = parser.parse_args()

    overnight_contract: ModelOvernightContract | None = None
    contract_path: Path | None = None
    if args.contract_file:
        contract_path = Path(args.contract_file).expanduser().resolve()
        if not contract_path.exists():
            raise FileNotFoundError(f"Contract file not found: {contract_path}")
        data = yaml.safe_load(contract_path.read_text())
        overnight_contract = ModelOvernightContract.model_validate(data)

    event_bus: EventPublisher | None = None
    if args.publish_events:
        event_bus = _build_kafka_publisher()

    command = ModelOvernightCommand(
        correlation_id=str(uuid.uuid4()),
        max_cycles=args.max_cycles,
        skip_build_loop=args.skip_build_loop,
        skip_merge_sweep=args.skip_merge_sweep,
        dry_run=args.dry_run,
        overnight_contract=overnight_contract,
    )

    handler = HandlerOvernight(
        event_bus=event_bus,
        contract_path=contract_path,
    )
    result = handler.handle(command, dispatch_phases=args.dispatch_phases)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.session_status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
