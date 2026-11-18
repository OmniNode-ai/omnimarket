# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one-pass serve tick of the stability lane refresh, run by a systemd timer as the operator account.

    python -m omnimarket.nodes.node_stability_lane_refresh_effect.runtime_stability_refresh_serve

A request published on the bus reaches the existing handler and its typed answer goes back on the bus: one
pass reads the request topic the contract names through the reader, answers every record through
``answer_command`` and publishes each of its events in order keyed by the correlation id the body carries.
The reader, publisher and handler are injectable parameters of ``run_once`` so tests need no broker;
``main`` builds the real ones from the overlay (the reader and publisher from its serve block, the handler
through ``build_handler``). Exit codes: 0 a pass answered nothing or every answered record ended on the
completed or refused topic; 1 an answered record ended on the failed topic (an unreadable record is
counted, not failed); 3 the overlay is missing or declares no serve block, or the bus cannot be read.
``--probe`` exits 0 before any overlay or bus read and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.adapters import (
    BusEventPublisher,
    BusRequestReader,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.handlers.handler_stability_lane_refresh import (
    HandlerStabilityLaneRefresh,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.models import (
    ModelStabilityServeEvidence,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.overlay import (
    ModelStabilityHostOverlay,
    load_overlay,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.protocols import (
    ProtocolBusEventPublisher,
    ProtocolBusRequestReader,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.runtime_stability_lane_refresh import (
    build_handler,
)

PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"
EXIT_OK = 0
EXIT_ANSWER_FAILED = 1
EXIT_UNREAD = 3


def _correlation_id(body: bytes) -> str | None:
    """The correlation id the event body carries, when it carries one."""
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    correlation = document.get("correlation_id") if isinstance(document, dict) else None
    return correlation if isinstance(correlation, str) else None


def run_once(
    overlay: ModelStabilityHostOverlay,
    handler: HandlerStabilityLaneRefresh,
    reader: ProtocolBusRequestReader,
    publisher: ProtocolBusEventPublisher,
) -> ModelStabilityServeEvidence:
    """One pass: read the request topic, answer each record, publish the events in order, record evidence.

    The overlay is the pass's declaration and carries the serve block; the handler holds its own copy of
    the host's facts. A record that does not parse is answered with one bad-payload event only and counts
    as unreadable, never as answered: the exit code turns 1 only when an answered record ended on the
    failed topic.
    """
    topics = load_topics()
    request_topic = topics["stability_refresh_requested"]
    raw_records = reader.read(request_topic)
    answered = unreadable = completed = refused = failed = 0
    correlation_ids: list[str] = []
    for raw in raw_records:
        events = answer_command(raw, handler, topics)
        if len(events) == 1 and events[0][0] == topics["stability_refresh_failed"]:
            unreadable += 1
        else:
            answered += 1
            terminal = events[-1][0]
            if terminal == topics["stability_refresh_completed"]:
                completed += 1
            elif terminal == topics["stability_refresh_refused"]:
                refused += 1
            elif terminal == topics["stability_refresh_failed"]:
                failed += 1
        record_ids: list[str] = []
        for topic, body in events:
            key = _correlation_id(body)
            publisher.publish(topic, body, key)
            if key is not None:
                record_ids.append(key)
        if record_ids:
            correlation_ids.append(record_ids[0])
    return ModelStabilityServeEvidence(
        records=len(raw_records),
        answered=answered,
        unreadable=unreadable,
        completed=completed,
        refused=refused,
        failed=failed,
        topic=request_topic,
        exit_code=EXIT_ANSWER_FAILED if failed else EXIT_OK,
        correlation_ids=tuple(correlation_ids),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_stability_lane_refresh_effect.runtime_stability_refresh_serve",
        description=(
            "One pass of the stability lane refresh serve tick: answer the requested refreshes "
            "on the bus and publish each typed answer"
        ),
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help=(
            "Start check: exit 0 and print one status line without loading an overlay, "
            "reading the bus or writing a file"
        ),
    )
    add_tick_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """One pass, or the probe: print one JSON line and write the unit heartbeat with the state dir."""
    arguments = _parser().parse_args(argv)
    if arguments.probe:
        sys.stdout.write(json.dumps({"status": PROBE_STATUS}, sort_keys=True) + "\n")
        return EXIT_OK
    line: dict[str, object]
    try:
        overlay = load_overlay()
    except NodeOverlayError as error:
        code, line = EXIT_UNREAD, {"status": "overlay_refused", "detail": str(error)}
    else:
        if overlay.serve is None:
            code, line = EXIT_UNREAD, {"status": "serve_not_declared"}
        else:
            try:
                evidence = run_once(
                    overlay,
                    build_handler(),
                    BusRequestReader(overlay.serve),
                    BusEventPublisher(overlay.serve),
                )
            except Exception as error:  # a bus that cannot be read
                code, line = (
                    EXIT_UNREAD,
                    {
                        "status": "source_unread",
                        "detail": f"{type(error).__name__}: {error}",
                    },
                )
            else:
                status = "no_records" if evidence.records == 0 else "served"
                code = evidence.exit_code
                line = {"status": status, **evidence.model_dump()}
    write_tick_heartbeat(arguments.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


__all__ = ["main", "run_once"]

if __name__ == "__main__":
    raise SystemExit(main())
