# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_broker_load_reading_effect <verb>``.

``daily``  read the broker's load and append the row.
``probe``  the same read, appending nothing.

Exit codes: 0 the read ran and its row was written (NOT_WORKING is still 0: a reading changes nothing and
refuses nothing); 1 the read or its row failed (an unreadable broker or probe, an append that did not land);
3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_broker_load_reading_effect.adapters.consumer_start import (
    InMemoryConsumerStart,
    KafkaConsumerStart,
)
from omnimarket.nodes.node_broker_load_reading_effect.adapters.metrics_source import (
    SshBrokerMetrics,
)
from omnimarket.nodes.node_broker_load_reading_effect.handlers.handler_broker_load import (
    HandlerBrokerLoad,
)
from omnimarket.nodes.node_broker_load_reading_effect.models.model_broker_load import (
    EnumConsumerStart,
    ModelBrokerLoadCommand,
    ModelConsumerStart,
)
from omnimarket.nodes.node_broker_load_reading_effect.overlay import (
    NODE_NAME,
    ModelBrokerLoadOverlay,
    load_overlay,
)
from omnimarket.nodes.node_broker_load_reading_effect.protocols import (
    ProtocolConsumerStart,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def build_handler(
    overlay: ModelBrokerLoadOverlay, environ: Mapping[str, str], write: bool
) -> tuple[HandlerBrokerLoad, str]:
    host = _required(overlay.ledger_append.host_env, environ)
    _required(overlay.metrics_source.ssh_host_env, environ)
    consumer: ProtocolConsumerStart = (
        KafkaConsumerStart(overlay.consumer)
        if overlay.consumer.backend == "kafka"
        else InMemoryConsumerStart(
            ModelConsumerStart(outcome=EnumConsumerStart.STARTED, elapsed_ms=0)
        )
    )
    handler = HandlerBrokerLoad(
        overlay=overlay,
        host=host,
        metrics=SshBrokerMetrics(overlay.metrics_source, environ),
        consumer=consumer,
        appender=build_appender(overlay.ledger_append, overlay.publish.lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )
    return handler, host


def _run(write: bool) -> int:
    overlay = load_overlay()
    handler, host = build_handler(overlay, os.environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelBrokerLoadCommand(correlation_id=f"broker-load-{host}-{now}", write=write)
    )
    if result.status == "failed":
        sys.stderr.write(f"BROKER-LOAD-FAILED {result.failure}\n")
        return 1
    metrics = result.metrics
    consumer = result.consumer
    sys.stdout.write(
        f"READING host={result.host} verdict={result.verdict.value} "
        f"outcome={result.outcome.value} "
        f"topics={metrics.topics if metrics else 'unknown'} "
        f"partitions={metrics.partitions if metrics else 'unknown'} "
        f"shards={metrics.shards if metrics else 'unknown'} "
        f"partitions_per_shard={result.partitions_per_shard} "
        f"empty_partitions={metrics.empty_partitions if metrics else 'unknown'} "
        f"shards_needed={result.shards_needed} partitions_over_bar={result.partitions_over_bar} "
        f"consumer_start={consumer.outcome.value if consumer else 'unknown'} "
        f"consumer_start_ms={consumer.elapsed_ms if consumer else 'none'} "
        f"ledger={result.ledger} key={result.row_key} reason={','.join(result.reasons)}\n"
    )
    return 1 if result.failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("daily", "probe"))
        args = parser.parse_args(arguments)
        return _run(write=args.verb == "daily")
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
