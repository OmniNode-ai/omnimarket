# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_down_reading_effect <verb>``.

``sweep``            probe every available lab host, write and publish the readings.
``down``             print the hosts the store reads as down, one per line.
``status --host H``  print the host's latest record as JSON.

Exit codes: 0 done; 1 refused or failed; 3 a deployment fact is missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.fakes import (
    FakeAdvertisementSource,
    FakeHostProber,
    InMemoryReadingPublisher,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.probes import (
    CommandAdvertisementSource,
    TcpHostProber,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.publisher import (
    BusReadingPublisher,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.stores import (
    InMemoryDownStore,
    JsonlDownStore,
)
from omnimarket.nodes.node_host_down_reading_effect.handlers.handler_host_down_reading import (
    HandlerHostDownReading,
)
from omnimarket.nodes.node_host_down_reading_effect.models.model_host_down_reading import (
    EnumSweepOutcome,
    ModelHostDownSweepRequest,
)
from omnimarket.nodes.node_host_down_reading_effect.overlay import (
    ModelHostDownOverlay,
    load_overlay,
)
from omnimarket.nodes.node_host_down_reading_effect.protocols import (
    ProtocolDownStore,
    ProtocolReadingPublisher,
)


def reading_topic() -> str:
    """The topic a reading goes on, read from this node's contract: no topic string lives in code."""
    text = (
        resources.files(__package__)
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    topic = (yaml.safe_load(text) or {}).get("topics", {}).get("host_down_reading")
    if not isinstance(topic, str) or not topic:
        raise _MissingFactError("the contract declares no host_down_reading topic")
    return topic


class _MissingFactError(ValueError):
    """A deployment fact the profile needs is not in the environment."""


def _require_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise _MissingFactError(f"environment variable {name} is not set")
    return value


def build_ports(
    overlay: ModelHostDownOverlay, environ: Mapping[str, str]
) -> tuple[
    ProtocolDownStore,
    ProtocolReadingPublisher,
    ProtocolEmissionSink,
    Literal["in_memory", "real"],
]:
    """The store, publisher and sink of the overlay's profile, and the backend label."""
    if overlay.profile == "in_memory":
        store: ProtocolDownStore = InMemoryDownStore()
        publisher: ProtocolReadingPublisher = InMemoryReadingPublisher()
        sink: ProtocolEmissionSink = InMemoryEmissionSink()
        return store, publisher, sink, "in_memory"
    home = Path(_require_env(environ, overlay.emission.home_env))
    sink = JsonlEmissionSink(home / overlay.emission.journal_relpath)
    store = JsonlDownStore(home / overlay.jsonl_state_relpath)
    if overlay.publish.bus == "kafka":
        from omnimarket.handlers.overlay_store.bus_opener import (
            open_lane_bus,
            workspace_root,
        )

        publisher = BusReadingPublisher(
            lambda: open_lane_bus(
                bus="kafka",
                lane=None
                if overlay.publish.kafka_bootstrap
                else overlay.publish.bus_lane,
                kafka_bootstrap=overlay.publish.kafka_bootstrap,
                root=workspace_root(environ),
            ),
            overlay.publish.timeout_s,
        )
    else:
        publisher = InMemoryReadingPublisher()
    return store, publisher, sink, "real"


def down_hosts_of(
    overlay: ModelHostDownOverlay, environ: Mapping[str, str]
) -> tuple[str, ...]:
    """The hosts the overlay's down store reads as down. A store that cannot be read raises."""
    store, _publisher, _sink, _backend = build_ports(overlay, environ)
    return store.down_hosts(datetime.now(UTC), overlay.reading_ttl_s)


def read_down_hosts(
    environ: Mapping[str, str] | None = None, now: datetime | None = None
) -> tuple[str, ...]:
    """The down hosts for a placement caller.

    No down overlay selected means no down verb is deployed, so no host can be down: the answer is empty.
    A broken overlay, a missing deployment fact or an unreadable store raises.
    """
    env = os.environ if environ is None else environ
    try:
        overlay = load_overlay(env)
    except NodeOverlayNotFoundError:
        return ()
    store, _publisher, _sink, _backend = build_ports(overlay, env)
    return store.down_hosts(now or datetime.now(UTC), overlay.reading_ttl_s)


def _print(model: BaseModel) -> None:
    sys.stdout.write(model.model_dump_json() + "\n")


def run(
    argv: Sequence[str],
    *,
    overlay: ModelHostDownOverlay,
    environ: Mapping[str, str],
) -> int:
    parser = argparse.ArgumentParser(prog="node_host_down_reading_effect")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("sweep")
    sub.add_parser("down")
    status = sub.add_parser("status")
    status.add_argument("--host", required=True)
    args = parser.parse_args(list(argv))

    try:
        store, publisher, sink, backend = build_ports(overlay, environ)
    except _MissingFactError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3

    if args.verb == "down":
        for host in store.down_hosts(datetime.now(UTC), overlay.reading_ttl_s):
            sys.stdout.write(host + "\n")
        return 0

    if args.verb == "status":
        record = store.latest(args.host)
        if record is None:
            sys.stdout.write("{}\n")
            return 0
        _print(record)
        return 0

    # sweep
    from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
        load_host_table,
    )

    observer = _require_env(environ, overlay.observer_env)
    handler = HandlerHostDownReading(
        settings=overlay,
        table_source=lambda: load_host_table(environ),
        prober=(TcpHostProber() if overlay.profile == "jsonl" else FakeHostProber({})),
        advertisements=(
            CommandAdvertisementSource(
                overlay.advertisement_command, overlay.advertisement_timeout_s
            )
            if overlay.advertisement_command is not None
            else FakeAdvertisementSource({})
        ),
        store=store,
        publisher=publisher,
        reading_topic=reading_topic(),
        sink=sink,
        clock=lambda: datetime.now(UTC),
        backend=backend,
        observer=observer,
    )
    result = handler.handle(ModelHostDownSweepRequest())
    _print(result)
    return 0 if result.outcome is EnumSweepOutcome.SWEPT else 1


def main(argv: Sequence[str] | None = None) -> int:
    environ = os.environ
    try:
        overlay = load_overlay(environ)
    except (NodeOverlayError, ValidationError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    return run(sys.argv[1:] if argv is None else argv, overlay=overlay, environ=environ)
