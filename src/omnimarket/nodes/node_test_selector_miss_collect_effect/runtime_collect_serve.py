# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one-pass serve tick of the nightly collector, run by a systemd timer on the host the overlay names.

    python -m omnimarket.nodes.node_test_selector_miss_collect_effect.runtime_collect_serve

The nightly sender publishes ``collect_nightly`` in an event envelope on the contract's command topic and waits for the
terminal event that names it. The node subscribes to nothing (its handler takes ports as constructor parameters the
runtime cannot build), so this process is the consumer: one pass reads the command topic through the reader, runs each
command through ``HandlerCollectNightly`` built with real ports (the repository gateway, a first-parent commit range, a
durable alarm store and the alarm routing's delivery) and publishes the result in an envelope that names the command by
its correlation id, which is the match the sender waits for. The reader, publisher and handler are injectable parameters
of ``run_once`` so tests need no broker; ``main`` builds the real ones from the overlay's serve block. Only the collect
operation is served here: ``check_slot_result`` and ``publish_attribution_record`` need ports this node does not yet ship
(a record store and the gateway's check-run write).

Exit codes: 0 a pass answered nothing or answered every record; 1 a record's handler failed; 3 the overlay is missing or
declares no serve block, or the bus cannot be read. ``--probe`` exits 0 before any overlay or bus read and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import envelope_bytes_for
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers.handler_collect_nightly import (
    HandlerCollectNightly,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelCollectRequest,
    ModelMissCollectSettings,
    load_policy,
)

NODE_NAME = "node_test_selector_miss_collect_effect"
OVERLAY_ENV = "ONEX_TEST_SELECTOR_MISS_COLLECT_OVERLAY"
CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"
PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"
EXIT_OK = 0
EXIT_ANSWER_FAILED = 1
EXIT_UNREAD = 3


class ProtocolCommandReader(Protocol):
    def read(self, topic: str) -> list[str]: ...


class ProtocolAnswerPublisher(Protocol):
    def publish(self, topic: str, body: bytes, key: str | None = None) -> None: ...


class ModelCollectServeEvidence(BaseModel):
    """What one pass did, written to the unit heartbeat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    records: int
    answered: int
    unreadable: int
    failed: int
    exit_code: int
    statuses: tuple[str, ...] = ()
    correlation_ids: tuple[str, ...] = ()


def load_topics(path: Path = CONTRACT_PATH) -> tuple[str, str]:
    """The collect command topic and the terminal topic, from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return (
        str(contract["runtime_dispatch"]["command_topic"]),
        str(contract["terminal_event"]),
    )


def _parse(raw: str) -> tuple[ModelCollectRequest, uuid.UUID] | None:
    """The request and correlation id a command envelope carries, or None when it is neither."""
    try:
        document: Any = json.loads(raw)
        payload = document.get("payload", document)
        correlation = uuid.UUID(str(document["correlation_id"]))
        return ModelCollectRequest.model_validate(payload), correlation
    except (ValueError, KeyError, AttributeError, TypeError, ValidationError):
        return None


def run_once(
    handler: HandlerCollectNightly,
    reader: ProtocolCommandReader,
    publisher: ProtocolAnswerPublisher,
) -> ModelCollectServeEvidence:
    """One pass: read the command topic, answer each command with a terminal event that names it."""
    command_topic, terminal_topic = load_topics()
    records = reader.read(command_topic)
    answered = unreadable = failed = 0
    statuses: list[str] = []
    correlation_ids: list[str] = []
    for raw in records:
        parsed = _parse(raw)
        if parsed is None:
            unreadable += 1
            continue
        request, correlation = parsed
        try:
            result = handler.handle(request)
        except Exception:  # a handler failure ends this record unanswered; the sender times out and reads it missed
            failed += 1
            continue
        publisher.publish(
            terminal_topic,
            envelope_bytes_for(
                terminal_topic, result.model_dump(mode="json"), correlation
            ),
            str(correlation),
        )
        answered += 1
        statuses.append(result.status.value)
        correlation_ids.append(str(correlation))
    return ModelCollectServeEvidence(
        topic=command_topic,
        records=len(records),
        answered=answered,
        unreadable=unreadable,
        failed=failed,
        exit_code=EXIT_ANSWER_FAILED if failed else EXIT_OK,
        statuses=tuple(statuses),
        correlation_ids=tuple(correlation_ids),
    )


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


def build_handler(settings: ModelMissCollectSettings) -> HandlerCollectNightly:
    """The collect handler with real ports; the serve block is required."""
    if settings.serve is None:
        raise ValueError("the overlay declares no serve block")
    from omnimarket.handlers.scm.handler_gateway_read_client import (
        HandlerGatewayReadClient,
    )
    from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver_route import (
        HandlerLabAlarmDeliverRoute,
    )
    from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.file_alarm_store import (
        FileAlarmStore,
    )
    from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.gateway_adapter import (
        GatewayNightlyGateway,
    )
    from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.git_commit_range import (
        GitCommitRange,
    )
    from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.routed_receiver import (
        RoutedAlarmReceiver,
    )

    policy = load_policy()
    return HandlerCollectNightly(
        settings=settings,
        policy=policy,
        gateway=GatewayNightlyGateway(
            reader=HandlerGatewayReadClient(),
            commits=GitCommitRange(_expand(settings.serve.commit_clone)),
            settings=settings,
            policy=policy,
        ),
        store=FileAlarmStore(_expand(settings.serve.state_dir)),
        receiver=RoutedAlarmReceiver(HandlerLabAlarmDeliverRoute()),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"omnimarket.nodes.{NODE_NAME}.runtime_collect_serve",
        description="One pass of the nightly collector's serve tick: answer the collect_nightly commands on the bus",
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help="Start check: exit 0 and print one status line without loading an overlay, reading the bus or writing a file",
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
        settings = load_structured_overlay(
            NODE_NAME, ModelMissCollectSettings, file_env=OVERLAY_ENV
        )
    except NodeOverlayError as error:
        code, line = EXIT_UNREAD, {"status": "overlay_refused", "detail": str(error)}
    else:
        if settings.serve is None:
            code, line = EXIT_UNREAD, {"status": "serve_not_declared"}
        else:
            from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.adapter_bus_serve import (
                BusAnswerPublisher,
                BusCommandReader,
            )

            try:
                evidence = run_once(
                    build_handler(settings),
                    BusCommandReader(settings.serve),
                    BusAnswerPublisher(settings.serve),
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
                code = evidence.exit_code
                line = {
                    "status": "no_records" if evidence.records == 0 else "served",
                    **evidence.model_dump(),
                }
    write_tick_heartbeat(arguments.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


__all__: list[str] = ["build_handler", "main", "run_once"]

if __name__ == "__main__":
    raise SystemExit(main())
