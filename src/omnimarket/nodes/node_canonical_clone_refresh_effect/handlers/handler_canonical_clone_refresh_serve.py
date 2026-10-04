# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-host serve process and the publish side of merge-triggered clone refresh (OMN-20496).

Every host that keeps canonical clones (the operator Mac and each lab pool host) runs one
``serve`` process. It reads the merge topic in a consumer group of its own (the node's group plus
the host name), so every host sees every merge, at offset ``latest``: a merge it missed while down
is the 180 s canonical-clone timer's job, not a backlog to replay.

COALESCING. A burst of merges to one repository is one sync, not one per merge: while a sync of a
repository runs, further merges of it only mark it dirty, and exactly one more sync runs after the
current one ends. Repositories sync independently, one sync per repository at a time. Each run
publishes one receipt naming the host, the repository, the engine's result, the before and after
sha and how many merge events it covered.

Producers (the landing controller after its merge readback, the PR watcher for a merge it saw and
did not make) publish one ``ModelRepoMerged`` with ``publish``. A payload that is not a readable
merge of an allowed repository is skipped and counted, never synced.

Topics come only from the node's ``contract.yaml``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Protocol

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
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.lab_work.bus import _bytes, _subscribe
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_canonical_clone_refresh_effect import (
    HandlerCanonicalCloneRefreshEffect,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_canonical_clone_refresh import (
    ModelCanonicalCloneRefreshReceipt,
    ModelCanonicalCloneRefreshRequest,
    ModelCanonicalCloneRefreshTopics,
    ModelRepoMerged,
)

logger = logging.getLogger(__name__)
NODE = "node_canonical_clone_refresh_effect"
GROUP_SERVICE = "omnimarket"


class ProtocolCloneRefreshHandler(Protocol):
    @property
    def host_name(self) -> str: ...

    def handle(
        self, request: ModelCanonicalCloneRefreshRequest
    ) -> ModelCanonicalCloneRefreshReceipt: ...


def load_canonical_clone_refresh_topics() -> ModelCanonicalCloneRefreshTopics:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    dispatch = yaml.safe_load(text)["runtime_dispatch"]
    return ModelCanonicalCloneRefreshTopics(
        merged=dispatch["subscribe_topic"],
        receipt=dispatch["terminal_events"]["success"],
    )


def host_group_id(host_name: str) -> str:
    """One consumer group per host: every host sees every merge."""
    return f"{derive_service_group_id(NODE, service=GROUP_SERVICE)}.{host_name}"


async def publish_repo_merged(
    bus: ProtocolLabRunBus,
    event: ModelRepoMerged,
    *,
    topics: ModelCanonicalCloneRefreshTopics | None = None,
) -> None:
    """Publish one merge event, keyed by repository so a repository's merges stay in order."""
    topic = (topics or load_canonical_clone_refresh_topics()).merged
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=event.model_dump(mode="json"), event_type=event_type_for(topic)
    )
    await bus.publish(topic, event.repo.encode("utf-8"), _bytes(envelope))


class _RepoSlot:
    """The coalescing state of one repository on this host."""

    def __init__(self) -> None:
        self.running = False
        self.events = 0
        self.target_sha = ""
        self.sources: list[str] = []


class CanonicalCloneRefreshHost:
    """Sync this host's clones of each merged repository, coalescing bursts."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: ProtocolCloneRefreshHandler,
        *,
        topics: ModelCanonicalCloneRefreshTopics | None = None,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._topics = topics or load_canonical_clone_refresh_topics()
        self._slots: dict[str, _RepoSlot] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubscribe: Callable[[], Awaitable[None]] | None = None
        self.skipped = 0
        self.runs = 0

    @property
    def topics(self) -> ModelCanonicalCloneRefreshTopics:
        return self._topics

    async def start(self) -> None:
        if self._unsubscribe is not None:
            return
        self._unsubscribe = await _subscribe(
            self._bus,
            self._topics.merged,
            self._on_message,
            host_group_id(self._handler.host_name),
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

    def _skip(self, why: str) -> None:
        self.skipped += 1
        logger.info("clone-refresh host %s skipped: %s", self._handler.host_name, why)

    async def _on_message(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._skip(f"unreadable event: {exc}")
            return
        payload = raw.get("payload", raw) if isinstance(raw, dict) else None
        try:
            event = ModelRepoMerged.model_validate(payload)
        except ValidationError as exc:
            self._skip(f"not a repo-merged event: {exc.error_count()} error(s)")
            return
        if event.state != "merged":
            self._skip(f"{event.repo}#{event.pr_number} is {event.state}, not merged")
            return
        slot = self._slots.setdefault(event.repo, _RepoSlot())
        slot.events += 1
        slot.target_sha = event.merge_sha
        slot.sources.append(f"{event.source}:{event.pr_number}")
        if not slot.running:
            slot.running = True
            task = asyncio.create_task(self._run(event.repo, slot))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run(self, repo: str, slot: _RepoSlot) -> None:
        try:
            while slot.events:
                request = ModelCanonicalCloneRefreshRequest(
                    repo=repo,
                    target_sha=slot.target_sha,
                    events=slot.events,
                    sources=slot.sources[-64:],
                )
                slot.events, slot.sources = 0, []
                self.runs += 1
                try:
                    receipt = await asyncio.to_thread(self._handler.handle, request)
                except (OSError, RuntimeError, ValueError):
                    # One failed sync must not stop this host's next merge of the repository.
                    logger.exception("clone-refresh host: sync of %s raised", repo)
                    continue
                await self._publish_receipt(receipt)
        finally:
            slot.running = False

    async def _publish_receipt(
        self, receipt: ModelCanonicalCloneRefreshReceipt
    ) -> None:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_dump(mode="json"),
            event_type=event_type_for(self._topics.receipt),
        )
        await self._bus.publish(
            self._topics.receipt, receipt.repo.encode("utf-8"), _bytes(envelope)
        )
        logger.info(
            "clone-refresh host %s %s %s %s->%s events=%d",
            receipt.host,
            receipt.repo,
            receipt.status,
            receipt.before_sha,
            receipt.after_sha,
            receipt.events_coalesced,
        )


@click.group("clone-refresh")
@click.pass_context
def clone_refresh_group(ctx: click.Context) -> None:
    """Refresh every host's canonical clone of a repository when it merges."""
    ctx.ensure_object(dict)


@clone_refresh_group.command("serve")
@_with_bus_options
@click.option(
    "--host-name", required=True, help="This host's name (a deployment fact)."
)
@click.option(
    "--sync-command",
    required=True,
    help="Shell-quoted argv of this host's sync command, naming the repository as {repo}.",
)
def serve_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    host_name: str,
    sync_command: str,
) -> None:
    """Serve merge events on this host until SIGTERM."""
    try:
        handler = HandlerCanonicalCloneRefreshEffect(
            shlex.split(sync_command), host_name=host_name
        )
    except ValueError as exc:
        raise click.UsageError(f"--sync-command: {exc}") from exc
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            omni_home=omnibase_path,
        ) as opened:
            host = CanonicalCloneRefreshHost(opened, handler)
            await host.start()
            try:
                await stop.wait()
            finally:
                await host.stop()

    try:
        asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(69)


@clone_refresh_group.command("publish")
@_with_bus_options
@click.option("--repo", required=True, help="owner/name")
@click.option("--base", required=True)
@click.option("--merge-sha", required=True)
@click.option("--pr", "pr_number", required=True, type=int)
@click.option("--merged-at", default=None, help="ISO 8601 with a zone; default now")
@click.option(
    "--source", required=True, help="Who saw the merge: controller, watcher, ..."
)
def publish_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    repo: str,
    base: str,
    merge_sha: str,
    pr_number: int,
    merged_at: str | None,
    source: str,
) -> None:
    """Publish one merge event and exit 0 once the broker has it."""
    try:
        event = ModelRepoMerged(
            repo=repo,
            base=base,
            merge_sha=merge_sha,
            pr_number=pr_number,
            merged_at=datetime.fromisoformat(merged_at)
            if merged_at
            else datetime.now(UTC),
            source=source,
        )
    except (ValueError, ValidationError) as exc:
        raise click.UsageError(str(exc)) from exc

    async def main() -> None:
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            omni_home=omnibase_path,
        ) as opened:
            await publish_repo_merged(opened, event)

    try:
        asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(69)
    click.echo(event.model_dump_json())


def main() -> None:
    """The same group without the onex CLI."""
    clone_refresh_group()


if __name__ == "__main__":
    main()
