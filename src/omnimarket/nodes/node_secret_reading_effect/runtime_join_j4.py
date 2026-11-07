# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_secret_reading_effect.runtime_join_j4 <verb>``.

``run``    resolve the consumer's key by overlay, scan, emit the two chains and append the row.
``probe``  the same run, appending no row (the chains still go to the emission journal).

``--inject <break>`` swaps the consumer for a named injected break (the failing control); it is
refused unless the join overlay is a drill overlay. The result prints as one JSON object with no
value in it. Exit codes: 0 the join read WORKING; 1 it read NOT WORKING or the run failed; 3 a
deployment fact is missing (no overlay, no host name, no append route).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.model_secret_join_j4 import ModelJoinJ4Command
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import LocalExposureSurfaces
from omnimarket.nodes.node_secret_reading_effect.adapters.drill_consumers import (
    DRILL_CONSUMERS,
    LeakyLogConsumer,
    LeakyRowConsumer,
)
from omnimarket.nodes.node_secret_reading_effect.handlers.handler_secret_join_j4 import (
    HandlerSecretJoinJ4,
    OverlayKeyConsumer,
    ProtocolJ4Consumer,
    event_of,
)
from omnimarket.nodes.node_secret_reading_effect.overlay import load_overlay
from omnimarket.nodes.node_secret_reading_effect.overlay_join_j4 import (
    load_join_overlay,
)
from omnimarket.nodes.node_secret_reading_effect.runtime_secret_reading import (
    build_store,
)


def _consumer(inject: str | None, drill: bool) -> ProtocolJ4Consumer:
    if inject is None:
        return OverlayKeyConsumer()
    if not drill:
        raise ValueError("an injected break runs only under a drill overlay")
    breaks: dict[str, ProtocolJ4Consumer] = {
        "log-value": LeakyLogConsumer(),
        "row-value": LeakyRowConsumer(),
    }
    return breaks[inject]


def _log(line: str) -> None:
    sys.stderr.write(line + "\n")


def _held(line: str) -> None:
    """A drill's log line is held in process for the scan and goes nowhere else: an injected
    break may carry a real value, and a drill never puts one on a terminal or a file."""
    del line


def _log_sink(drill: bool) -> Callable[[str], None]:
    return _held if drill else _log


def _run(arguments: argparse.Namespace, write: bool) -> int:
    reading = load_overlay()
    join = load_join_overlay()
    host = os.environ.get(reading.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{reading.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    journal = Path(os.path.expandvars(os.path.expanduser(join.emission_journal)))
    handler = HandlerSecretJoinJ4(
        reading=reading,
        join=join,
        store=build_store(reading),
        surfaces=LocalExposureSurfaces(),
        sink=JsonlEmissionSink(journal),
        appender=build_appender(reading.ledger_append, join.lane) if write else None,
        consumer=_consumer(arguments.inject, join.drill),
        log=_log_sink(join.drill),
        clock=lambda: datetime.now(UTC),
        host=host,
    )
    result = handler.handle(
        ModelJoinJ4Command(
            correlation_id=arguments.correlation_id or uuid.uuid4().hex, write=write
        )
    )
    # The body names outcomes and surface kinds only; it goes out as bytes on the event stream,
    # which is not a log sink.
    sys.stdout.buffer.write(
        (json.dumps(event_of(result), sort_keys=True) + "\n").encode()
    )
    sys.stdout.buffer.flush()
    ok = result.status == "completed" and result.verdict is EnumReadingVerdict.WORKING
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_secret_reading_join_j4")
    parser.add_argument("verb", choices=("run", "probe"))
    parser.add_argument("--correlation-id")
    parser.add_argument("--inject", choices=sorted(DRILL_CONSUMERS))
    arguments = parser.parse_args(argv)
    try:
        return _run(arguments, write=arguments.verb == "run")
    except (NodeOverlayError, LedgerRouteError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
