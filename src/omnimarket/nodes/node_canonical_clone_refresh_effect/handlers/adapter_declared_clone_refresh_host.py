# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus side and the command of the declared-clone refresh (OMN-22149).

``DeclaredCloneRefreshHost`` reads the request topic, runs ``HandlerDeclaredCloneRefresh`` and
publishes one receipt on the receipt topic, on whichever bus it is given (in-memory or Kafka). The
topics come only from the node's ``contract.yaml``. ``clone-refresh declared`` wires the host from
the overlay (the host table and the repository's directory name), publishes one request, waits for
its receipt, prints it and exits non-zero when any host is a refusal.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path
from typing import Protocol

import click
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import (
    resolve_lab_hosts_table,
    resolve_overlay_key,
)
from omnimarket.lab_work.bus import _bytes, _subscribe
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_canonical_clone_refresh_serve import (
    NODE,
    clone_refresh_group,
    host_group_id,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
    HandlerDeclaredCloneRefresh,
    TransportSubprocess,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
    ModelDeclaredCloneRefreshReceipt,
    ModelDeclaredCloneRefreshRequest,
    ModelDeclaredCloneTopics,
)

logger = logging.getLogger(__name__)

# The opener's keyword for the registry root, read from its signature rather than spelled here.
_REGISTRY_KW = next(
    name
    for name in inspect.signature(open_lab_run_bus).parameters
    if name.endswith("_home")
)


class ProtocolDeclaredCloneHandler(Protocol):
    def handle(
        self, request: ModelDeclaredCloneRefreshRequest
    ) -> ModelDeclaredCloneRefreshReceipt: ...


def load_declared_clone_topics() -> ModelDeclaredCloneTopics:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    block = yaml.safe_load(text)["declared_clone_refresh"]
    return ModelDeclaredCloneTopics(
        request=block["request_topic"],
        receipt=block["receipt_topic"],
        repo_role=block["repo_role"],
    )


async def publish_declared_request(
    bus: ProtocolLabRunBus,
    request: ModelDeclaredCloneRefreshRequest,
    *,
    topics: ModelDeclaredCloneTopics | None = None,
) -> None:
    topic = (topics or load_declared_clone_topics()).request
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=request.model_dump(mode="json"), event_type=event_type_for(topic)
    )
    await bus.publish(topic, None, _bytes(envelope))


class DeclaredCloneRefreshHost:
    """Run a declared-clone refresh for each request on the request topic."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: ProtocolDeclaredCloneHandler,
        *,
        group_name: str,
        topics: ModelDeclaredCloneTopics | None = None,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._group = group_name
        self._topics = topics or load_declared_clone_topics()
        self._unsubscribe: Callable[[], Awaitable[None]] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.skipped = 0

    async def start(self) -> None:
        if self._unsubscribe is not None:
            return
        self._unsubscribe = await _subscribe(
            self._bus,
            self._topics.request,
            self._on_message,
            f"{host_group_id(self._group)}.declared",
            "latest",
        )

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            await self._unsubscribe()
            self._unsubscribe = None
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _on_message(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
            payload = raw.get("payload", raw) if isinstance(raw, dict) else None
            request = ModelDeclaredCloneRefreshRequest.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            self.skipped += 1
            logger.info("declared-clone host skipped an unreadable request: %s", exc)
            return
        task = asyncio.create_task(self._run(request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, request: ModelDeclaredCloneRefreshRequest) -> None:
        receipt = await asyncio.to_thread(self._handler.handle, request)
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_dump(mode="json"),
            event_type=event_type_for(self._topics.receipt),
        )
        await self._bus.publish(self._topics.receipt, None, _bytes(envelope))


async def run_declared_refresh(
    bus: ProtocolLabRunBus,
    handler: ProtocolDeclaredCloneHandler,
    request: ModelDeclaredCloneRefreshRequest,
    *,
    group_name: str,
    wait_seconds: float,
) -> ModelDeclaredCloneRefreshReceipt:
    """Publish one request, run the host on ``bus`` and return the receipt it published."""
    topics = load_declared_clone_topics()
    arrived: asyncio.Queue[ModelDeclaredCloneRefreshReceipt] = asyncio.Queue()

    async def on_receipt(message: ProtocolBusMessage) -> None:
        raw = json.loads(message.value)
        with contextlib.suppress(ValidationError):
            arrived.put_nowait(
                ModelDeclaredCloneRefreshReceipt.model_validate(raw.get("payload", raw))
            )

    unsubscribe = await _subscribe(
        bus,
        topics.receipt,
        on_receipt,
        f"{host_group_id(group_name)}.declared-cli",
        "latest",
    )
    host = DeclaredCloneRefreshHost(bus, handler, group_name=group_name, topics=topics)
    await host.start()
    try:
        await publish_declared_request(bus, request, topics=topics)
        return await asyncio.wait_for(arrived.get(), timeout=wait_seconds)
    finally:
        await host.stop()
        await unsubscribe()


@clone_refresh_group.command("declared")
@_with_bus_options
@click.option(
    "--host-name",
    required=True,
    help="This host's name (a deployment fact): its row runs locally, the others over ssh.",
)
@click.option(
    "--host",
    "hosts",
    multiple=True,
    help="Refresh only this host's declared clone (repeatable); default every declaring host.",
)
@click.option("--wait-seconds", default=600.0, show_default=True, type=float)
def declared_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    host_name: str,
    hosts: tuple[str, ...],
    wait_seconds: float,
) -> None:
    """Refresh the declared clone of each lab host; exit 1 when any host is a refusal."""
    topics = load_declared_clone_topics()
    try:
        table = resolve_lab_hosts_table()
        repo_dir = resolve_overlay_key(f"repos.{topics.repo_role}")
    except NodeOverlayError as exc:
        raise click.ClickException(str(exc)) from exc
    handler = HandlerDeclaredCloneRefresh(
        table, repo_dir, TransportSubprocess(local_host_name=host_name)
    )
    request = ModelDeclaredCloneRefreshRequest(hosts=hosts)

    async def main() -> ModelDeclaredCloneRefreshReceipt:
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            **{_REGISTRY_KW: omnibase_path},
        ) as opened:
            return await run_declared_refresh(
                opened,
                handler,
                request,
                group_name=host_name,
                wait_seconds=wait_seconds,
            )

    try:
        receipt = asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(69)
    except TimeoutError:
        click.echo("declared-clone refresh: no receipt arrived in time", err=True)
        sys.exit(75)
    click.echo(receipt.model_dump_json(indent=2))
    if receipt.refused:
        for outcome in receipt.outcomes:
            if outcome.refusal is not None:
                click.echo(
                    f"REFUSED host={outcome.host} path={outcome.path} "
                    f"reason={outcome.refusal.value}: {outcome.message}",
                    err=True,
                )
        sys.exit(1)
