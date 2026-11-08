# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-host serve process and the caller of the repository state workflow (OMN-23033).

``repo-state serve`` runs on every host that keeps canonical clones (the lab hosts and the
orchestrator Mac). It folds the merge events the bus carries into the latest state per
repository (a consumer group per host, from the earliest retained event on first start,
persisted so a restart keeps it), and answers every query addressed to its host on the
request topic: compare, pull only when behind, answer from the mirror. Each answer goes
to the success terminal when it is ``ok`` and to the failure terminal when it is not, as
an envelope whose ``parent_envelope_id`` is the query's own envelope id.

``repo-state query`` is what a lane, the landing controller, the floor and the lab fill
call. It reads the terminals with no consumer group (from the log end, the bus returning
only once every partition has a position), publishes one query keyed by host, and
prints the answer that names its envelope.

Topics come from the node's ``contract.yaml``; settings from its overlay.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path
from typing import Any

import click
import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, open_lab_run_bus
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.lab_work.bus import _bytes, _subscribe, _uuid_or_none
from omnimarket.nodes.node_canonical_clone_refresh_effect import (
    HandlerCanonicalCloneRefreshEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import require_ungrouped
from omnimarket.nodes.node_repo_state_query_effect.handlers.adapter_repo_state_mirrors import (
    CommandMirrorPuller,
    FoldedBusRepoState,
    GitCloneMirror,
    PrWatcherStateFile,
    clones_under,
)
from omnimarket.nodes.node_repo_state_query_effect.handlers.handler_repo_state_query import (
    HandlerRepoStateQuery,
)
from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_overlay import (
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    ModelRepoStateOverlay,
)
from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    EnumRepoStateWant,
    ModelRepoStateAnswer,
    ModelRepoStateQuery,
    ModelRepoStateTopics,
)

logger = logging.getLogger(__name__)
NODE = "node_repo_state_query_effect"
GROUP_SERVICE = "omnimarket"
EXIT_NOT_OK = 1
EXIT_TIMEOUT = 124
EXIT_BUS = 69
EXIT_CONFIG = 78


def load_repo_state_topics() -> ModelRepoStateTopics:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    contract = yaml.safe_load(text)
    dispatch = contract["runtime_dispatch"]
    return ModelRepoStateTopics(
        request=dispatch["command_topic"],
        success=dispatch["terminal_events"]["success"],
        failure=dispatch["terminal_events"]["failure"],
        bus_state=tuple(item["topic"] for item in contract["bus_state_events"]),
    )


def host_group_id(host_name: str, role: str) -> str:
    """One consumer group per host and role: every host sees every query and every merge."""
    return f"{derive_service_group_id(NODE, service=GROUP_SERVICE)}.{role}.{host_name}"


def load_overlay(path: Path | None = None) -> ModelRepoStateOverlay:
    explicit = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    return load_structured_overlay(
        OVERLAY_ID, ModelRepoStateOverlay, explicit=explicit, file_env=OVERLAY_FILE_ENV
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise NodeOverlayError(
            f"the overlay names {name}, and it is unset on this host"
        )
    return value


def build_handler(
    overlay: ModelRepoStateOverlay, host_name: str
) -> tuple[HandlerRepoStateQuery, FoldedBusRepoState]:
    """The deployed handler, every dependency built from the overlay."""
    bus_state = FoldedBusRepoState(
        Path(_required_env(overlay.state_dir_env)) / overlay.bus_state_file
    )
    clone_refresh = HandlerCanonicalCloneRefreshEffect(
        shlex.split(_required_env(overlay.sync_command_env)),
        host_name=host_name,
        timeout_seconds=overlay.pull_timeout_seconds,
    )
    watcher_cmd = os.environ.get(overlay.watcher_refresh_command_env, "").strip()
    handler = HandlerRepoStateQuery(
        host_name=host_name,
        clone=GitCloneMirror(
            clones_under(Path(_required_env(overlay.clones_root_env))),
            timeout_seconds=overlay.git_timeout_seconds,
            max_file_chars=overlay.max_file_chars,
        ),
        watcher=PrWatcherStateFile(Path(_required_env(overlay.watcher_state_env))),
        bus_state=bus_state,
        puller=CommandMirrorPuller(
            clone_refresh,
            watcher_argv=shlex.split(watcher_cmd) if watcher_cmd else None,
            timeout_seconds=overlay.pull_timeout_seconds,
        ),
    )
    return handler, bus_state


class RepoStateHost:
    """Fold the bus's merge events and answer this host's queries."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: HandlerRepoStateQuery,
        bus_state: FoldedBusRepoState,
        *,
        topics: ModelRepoStateTopics | None = None,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._state = bus_state
        self._topics = topics or load_repo_state_topics()
        self._unsubscribes: list[Callable[[], Awaitable[None]]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self.skipped = 0

    @property
    def topics(self) -> ModelRepoStateTopics:
        return self._topics

    async def start(self) -> None:
        host = self._handler.host_name
        for topic in self._topics.bus_state:
            self._unsubscribes.append(
                await _subscribe(
                    self._bus,
                    topic,
                    self._on_state,
                    host_group_id(host, "state"),
                    "earliest",
                )
            )
        self._unsubscribes.append(
            await _subscribe(
                self._bus,
                self._topics.request,
                self._on_request,
                host_group_id(host, "query"),
                "latest",
            )
        )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            await unsubscribe()
        self._unsubscribes.clear()
        await self.drain()

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _on_state(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.skipped += 1
            return
        payload = raw.get("payload", raw) if isinstance(raw, dict) else None
        if not isinstance(payload, dict):
            self.skipped += 1
            return
        self._state.apply(payload)

    async def _on_request(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.skipped += 1
            return
        if not isinstance(raw, dict):
            self.skipped += 1
            return
        payload = raw.get("payload", raw)
        if isinstance(payload, dict) and payload.get("host") != self._handler.host_name:
            return  # another host's query: seen by every host, answered by its own
        command_id = _uuid_or_none(raw.get("envelope_id"))
        try:
            request = ModelRepoStateQuery.model_validate(payload)
        except ValidationError as exc:
            await self._publish(
                self._topics.failure,
                {
                    "host": self._handler.host_name,
                    "ok": False,
                    "error": f"not a repo-state query: {exc.error_count()} error(s)",
                },
                command_id,
                None,
            )
            return
        # Each query runs on its own task, so concurrent queries can share one pull.
        task = asyncio.create_task(self._answer(request, command_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(
        self, request: ModelRepoStateQuery, command_id: uuid.UUID | None
    ) -> None:
        try:
            answer = await self._handler.handle(request)
        except Exception as exc:
            # A handler crash is answered on the failure terminal, never dropped.
            logger.exception("repo-state handler raised")
            await self._publish(
                self._topics.failure,
                {
                    "host": self._handler.host_name,
                    "repo": request.repo,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[-2000:],
                },
                command_id,
                request.correlation_id,
            )
            return
        topic = self._topics.success if answer.ok else self._topics.failure
        await self._publish(
            topic, answer.model_dump(mode="json"), command_id, request.correlation_id
        )

    async def _publish(
        self,
        topic: str,
        payload: dict[str, Any],
        command_id: uuid.UUID | None,
        correlation_id: uuid.UUID | None,
    ) -> None:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload,
            correlation_id=correlation_id or uuid.uuid4(),
            parent_envelope_id=command_id,
            event_type=event_type_for(topic),
        )
        key = str(payload.get("repo") or payload.get("host") or "").encode("utf-8")
        await self._bus.publish(topic, key, _bytes(envelope))


class RepoStateCaller:
    """Publish one query and read its answer, joining no consumer group."""

    def __init__(
        self, bus: ProtocolLabRunBus, *, topics: ModelRepoStateTopics | None = None
    ) -> None:
        self._bus = require_ungrouped(bus)
        self._topics = topics or load_repo_state_topics()
        self._pending: dict[uuid.UUID, asyncio.Future[tuple[str, dict[str, Any]]]] = {}
        self._unsubscribes: list[Callable[[], Awaitable[None]]] = []

    async def start(self) -> None:
        for topic in (self._topics.success, self._topics.failure):
            self._unsubscribes.append(
                await self._bus.subscribe_ungrouped(topic, self._terminal(topic))
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            await unsubscribe()
        self._unsubscribes.clear()

    def _terminal(self, topic: str) -> Callable[[ProtocolBusMessage], Awaitable[None]]:
        async def on_message(message: ProtocolBusMessage) -> None:
            try:
                raw = json.loads(message.value)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(raw, dict):
                return
            parent = _uuid_or_none(raw.get("parent_envelope_id"))
            future = self._pending.get(parent) if parent else None
            if future is not None and not future.done():
                future.set_result((topic, raw))

        return on_message

    async def query(
        self, request: ModelRepoStateQuery, *, timeout_seconds: float
    ) -> ModelRepoStateAnswer | dict[str, Any] | None:
        """The typed answer, the failure payload when it is not one, or None at the timeout."""
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=request.model_dump(mode="json"),
            correlation_id=request.correlation_id,
            event_type=event_type_for(self._topics.request),
        )
        future: asyncio.Future[tuple[str, dict[str, Any]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[envelope.envelope_id] = future
        try:
            await self._bus.publish(
                self._topics.request, request.host.encode("utf-8"), _bytes(envelope)
            )
            _topic, raw = await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            return None
        finally:
            self._pending.pop(envelope.envelope_id, None)
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return {"ok": False, "error": "answer without a payload"}
        try:
            return ModelRepoStateAnswer.model_validate(payload)
        except ValidationError:
            return payload


@click.group("repo-state")
@click.pass_context
def repo_state_group(ctx: click.Context) -> None:
    """Answer repository state from this host's mirror, pulling first only when it is behind."""
    ctx.ensure_object(dict)


_overlay_option = click.option(
    "--overlay",
    "overlay_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=f"Overlay file; else {OVERLAY_FILE_ENV}, else the overlay roots.",
)
_profile_option = click.option(
    "--profile",
    default=None,
    help="Bus profile from the overlay; else its default_profile.",
)


def _bus_args(
    overlay: ModelRepoStateOverlay, profile_name: str | None
) -> tuple[str, str | None, str | None, None]:
    """(bus, lane, kafka bootstrap, workspace root) for ``positional_opener``."""
    profile = overlay.profile(profile_name)
    env = profile.kafka_bootstrap_env
    bootstrap = os.environ.get(env, "").strip() if env else ""
    return (profile.kind, profile.lane, bootstrap or None, None)


@repo_state_group.command("serve")
@_overlay_option
@_profile_option
@click.option(
    "--host-name", required=True, help="This host's name (a deployment fact)."
)
def serve_command(
    overlay_path: Path | None, profile: str | None, host_name: str
) -> None:
    """Serve this host's repository state queries until SIGTERM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        overlay = load_overlay(overlay_path)
        handler, bus_state = build_handler(overlay, host_name)
        bus_args = _bus_args(overlay, profile)
    except (NodeOverlayError, ValueError) as exc:
        click.echo(f"overlay: {exc}", err=True)
        sys.exit(EXIT_CONFIG)

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        async with positional_opener(open_lab_run_bus)(*bus_args) as opened:
            host = RepoStateHost(opened, handler, bus_state)
            await host.start()
            logger.info("repo-state serve %s on %s", host_name, host.topics.request)
            try:
                await stop.wait()
            finally:
                await host.stop()

    try:
        asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(EXIT_BUS)


@repo_state_group.command("query")
@_overlay_option
@_profile_option
@click.option("--host", "host", required=True, help="The host whose mirror answers.")
@click.option("--repo", required=True, help="owner/name")
@click.option(
    "--want", required=True, type=click.Choice([w.value for w in EnumRepoStateWant])
)
@click.option("--branch", default=None)
@click.option(
    "--revision", default=None, help="A full sha or a branch name (want files)."
)
@click.option(
    "--path", "paths", multiple=True, help="A repository-relative path (want files)."
)
@click.option("--pr", "pr_number", type=int, default=None)
@click.option("--job-id", default=None)
def query_command(
    overlay_path: Path | None,
    profile: str | None,
    host: str,
    repo: str,
    want: str,
    branch: str | None,
    revision: str | None,
    paths: tuple[str, ...],
    pr_number: int | None,
    job_id: str | None,
) -> None:
    """Ask one host's mirror; print the answer; exit 0 when it is ok."""
    try:
        overlay = load_overlay(overlay_path)
        bus_args = _bus_args(overlay, profile)
        request = ModelRepoStateQuery(
            host=host,
            repo=repo,
            want=EnumRepoStateWant(want),
            branch=branch,
            revision=revision,
            paths=paths,
            pr_number=pr_number,
            job_id=job_id,
        )
    except (NodeOverlayError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc

    async def main() -> ModelRepoStateAnswer | dict[str, Any] | None:
        async with positional_opener(open_lab_run_bus)(*bus_args) as opened:
            caller = RepoStateCaller(opened)
            await caller.start()
            try:
                return await caller.query(
                    request, timeout_seconds=overlay.reply_timeout_seconds
                )
            finally:
                await caller.stop()

    try:
        answer = asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(EXIT_BUS)
    if answer is None:
        click.echo(
            f"no answer from {host} within {overlay.reply_timeout_seconds}s", err=True
        )
        sys.exit(EXIT_TIMEOUT)
    if isinstance(answer, ModelRepoStateAnswer):
        click.echo(answer.model_dump_json())
        sys.exit(0 if answer.ok else EXIT_NOT_OK)
    click.echo(json.dumps(answer))
    sys.exit(EXIT_NOT_OK)


def main() -> None:
    repo_state_group()


__all__ = [
    "RepoStateCaller",
    "RepoStateHost",
    "build_handler",
    "host_group_id",
    "load_overlay",
    "load_repo_state_topics",
    "repo_state_group",
]
