# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_store_read_effect``: refresh, read, compare, fleet.

Binds the handler to the backend the overlay selects. ``read``, ``compare`` and :func:`read_overlay`
touch neither broker nor database, so they answer when both are down.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.handlers.overlay_store.snapshot_root import FileOverlaySnapshot
from omnimarket.handlers.overlay_store.store_bus_snapshot import BusSnapshotOverlayStore
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import (
    EnumOverlayReadState,
    ModelOverlayDocumentRead,
    ModelOverlayStoreReading,
    ModelOverlayStoreReadOverlay,
)
from omnimarket.nodes.node_overlay_store_read_effect.handlers import (
    HandlerOverlayStoreRead,
    compare_documents,
)

NODE = "node_overlay_store_read_effect"
EXIT_UNREADABLE = 65
EXIT_DIFFERS = 1


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


def _snapshot(overlay: ModelOverlayStoreReadOverlay) -> FileOverlaySnapshot:
    return FileOverlaySnapshot(Path(os.path.expanduser(overlay.snapshot_root)))


class _BusReporter:
    """Publishes each reading on the contract's topic; the floor reads the newest of each host."""

    def __init__(self, open_bus: Any, topic: str) -> None:
        self._open_bus = open_bus
        self._topic = topic

    async def report(self, reading: ModelOverlayStoreReading) -> None:
        async with self._open_bus() as bus:
            await bus.publish(
                self._topic,
                f"{reading.host}:{reading.overlay_id}".encode(),
                reading.model_dump_json().encode("utf-8"),
            )


class _NoReporter:
    async def report(self, reading: ModelOverlayStoreReading) -> None:
        return None


def _in_memory_store(
    overlay: ModelOverlayStoreReadOverlay,
    host: str,
    kafka_bootstrap: str | None,
    env: Mapping[str, str],
) -> tuple[Any, Any]:
    return InMemoryOverlayStore(), _NoReporter()


def _postgres_store(
    overlay: ModelOverlayStoreReadOverlay,
    host: str,
    kafka_bootstrap: str | None,
    env: Mapping[str, str],
) -> tuple[Any, Any]:
    raise NodeOverlayError(
        f"{NODE}: the postgres backend belongs to a runtime, which binds its own pool"
    )


def _bus_snapshot_store(
    overlay: ModelOverlayStoreReadOverlay,
    host: str,
    kafka_bootstrap: str | None,
    env: Mapping[str, str],
) -> tuple[Any, Any]:
    contract = _contract()
    bootstrap = kafka_bootstrap or overlay.kafka_bootstrap

    def open_bus() -> Any:
        return open_lane_bus(
            bus="kafka",
            lane=None if bootstrap else overlay.bus_lane,
            kafka_bootstrap=bootstrap,
            root=workspace_root(env),
        )

    return (
        BusSnapshotOverlayStore(
            open_bus=open_bus,
            topic=contract["snapshot_read"]["topic"],
            group_prefix=f"{NODE}.{host}",
            window_s=overlay.read_window_s,
        ),
        _BusReporter(open_bus, contract["event_bus"]["publish_topics"][0]),
    )


_BACKENDS: Mapping[str, Callable[..., tuple[Any, Any]]] = {
    "in_memory": _in_memory_store,
    "postgres": _postgres_store,
    "bus_snapshot": _bus_snapshot_store,
}


def build_handler(
    overlay: ModelOverlayStoreReadOverlay,
    env: Mapping[str, str],
    *,
    kafka_bootstrap: str | None = None,
) -> HandlerOverlayStoreRead:
    """The handler the overlay selects. A runtime holds the postgres backend; a host never does."""
    host = env.get(overlay.host_name_env, "").strip()
    if not host:
        raise NodeOverlayError(f"{NODE}: {overlay.host_name_env} is not set")
    store, reporter = _BACKENDS[overlay.backend](overlay, host, kafka_bootstrap, env)
    return HandlerOverlayStoreRead(
        store=store,
        snapshot=_snapshot(overlay),
        reporter=reporter,
        host=host,
        policies=overlay.overlays,
    )


def read_overlay(
    overlay_id: str, environ: Mapping[str, str] | None = None
) -> ModelOverlayDocumentRead:
    """The one read a consumer takes. Touches no broker or database.

    Raises ``OverlayAbsentError`` with no usable snapshot, and ``OverlayStaleError`` past the bound of
    an overlay whose policy refuses.
    """
    env = os.environ if environ is None else environ
    overlay = load_structured_overlay(NODE, ModelOverlayStoreReadOverlay, env)
    return HandlerOverlayStoreRead(
        store=InMemoryOverlayStore(),
        snapshot=_snapshot(overlay),
        reporter=_NoReporter(),
        host=env.get(overlay.host_name_env, ""),
        policies=overlay.overlays,
    ).read_document(overlay_id)


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))  # noqa: T201


def exit_for(readings: tuple[ModelOverlayStoreReading, ...]) -> int:
    unreadable = any(r.state is EnumOverlayReadState.UNREADABLE for r in readings)
    return EXIT_UNREADABLE if unreadable else 0


async def fleet(
    overlay: ModelOverlayStoreReadOverlay,
    env: Mapping[str, str],
    kafka_bootstrap: str | None,
) -> dict[str, Any]:
    """The newest reading of each host and overlay on the bus: the floor row's source."""
    newest: dict[str, dict[str, Any]] = {}

    async def on_message(message: Any) -> None:
        reading = json.loads(bytes(message.value).decode("utf-8"))
        key = f"{reading['host']}:{reading['overlay_id']}"
        held = newest.get(key)
        if held is None or held["read_at"] <= reading["read_at"]:
            newest[key] = reading

    bootstrap = kafka_bootstrap or overlay.kafka_bootstrap
    async with open_lane_bus(
        bus="kafka",
        lane=None if bootstrap else overlay.bus_lane,
        kafka_bootstrap=bootstrap,
        root=workspace_root(env),
    ) as bus:
        stop = await bus.subscribe(
            _contract()["event_bus"]["publish_topics"][0],
            on_message=on_message,
            group_id=f"{NODE}.fleet.{uuid.uuid4().hex}",
            auto_offset_reset="earliest",
        )
        try:
            await asyncio.sleep(overlay.read_window_s)
        finally:
            await stop()
    return {"readings": newest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE, description=__doc__)
    parser.add_argument("verb", choices=("refresh", "read", "compare", "fleet"))
    parser.add_argument("--id", help="read, compare: the overlay id")
    parser.add_argument("--file", type=Path, help="compare: the source file")
    parser.add_argument(
        "--kafka-bootstrap",
        default=None,
        help="the broker, stated directly; default the overlay's lane",
    )
    parser.add_argument("--out", type=Path, help="fleet: also write the answer here")
    args = parser.parse_args(argv)
    env = os.environ
    try:
        overlay = load_structured_overlay(NODE, ModelOverlayStoreReadOverlay, env)
        handler = build_handler(overlay, env, kafka_bootstrap=args.kafka_bootstrap)
    except NodeOverlayError as exc:
        print(f"{NODE}: {exc}", file=sys.stderr)  # noqa: T201
        return EXIT_UNREADABLE
    if args.verb == "refresh":
        readings = asyncio.run(handler.refresh_all())
        _print([r.model_dump(mode="json") for r in readings])
        return exit_for(readings)
    if args.verb == "fleet":
        answer = asyncio.run(fleet(overlay, env, args.kafka_bootstrap))
        _print(answer)
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(answer, indent=2), encoding="utf-8")
        return 0
    if not args.id:
        parser.error(f"{args.verb} needs --id")
    if args.verb == "read":
        reading = handler.read(args.id)
        _print(reading.model_dump(mode="json"))
        return exit_for((reading,))
    if args.file is None:
        parser.error("compare needs --file")
    compared = compare_documents(
        yaml.safe_load(args.file.read_text(encoding="utf-8")),
        _snapshot(overlay).read_document(args.id),
    )
    _print(compared.model_dump(mode="json"))
    return 0 if compared.equal else EXIT_DIFFERS


__all__ = [
    "EXIT_UNREADABLE",
    "NODE",
    "build_handler",
    "exit_for",
    "fleet",
    "main",
    "read_overlay",
]
