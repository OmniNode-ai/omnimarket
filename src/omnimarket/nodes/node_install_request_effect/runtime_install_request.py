# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_install_request_effect serve|publish|owed``
(OMN-19998, root 5).

* :class:`InstallRequestHost` runs on the host that serves install requests. It
  reads the node's command topic behind a consumer group of its own (one per
  host, so every host sees every request and answers its own), hands each
  request to :class:`HandlerInstallRequest` on a worker thread -- an install
  runs minutes and the consumer must never block in its callback -- and
  publishes the typed result on the node's success terminal. A command that is
  no request, or a handler that raises, is answered on the failure terminal and
  logged, never swallowed. A held request (a blocker on the host) is kept and
  decided again after the overlay's retry interval.
* :func:`publish_request` is the lane side: it stamps the draft with its id and
  its expiry from the overlay, publishes it on the command topic and returns it.
* ``owed`` is the offline reading: it reads a file of published requests and
  the JSON-lines result journal, hands both to
  :class:`HandlerInstallRequestOwed` and prints one line per owed install --
  it opens no bus.

The host applies no command-age limit of its own: the request's own
``expires_at`` is the bound, and the decision compute expires what stopped
being worth running, so a retained command is answered or expired, never
dropped unnamed.

Exit codes: 0 the host served until it was stopped / the draft was published /
the owed reading was printed; 1 the bus could not be opened / an owed install
expired; 3 a deployment fact or a record is missing or unreadable
(INDETERMINATE).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import json
import logging
import os
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, cast

import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    envelope_bytes_for,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.install_request import (
    EnumInstallRequestAction,
    ModelInstallOwedInput,
    ModelInstallRequest,
    ModelInstallRequestDraft,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_effect.handlers.handler_install_request import (
    HandlerInstallRequest,
    JsonlResultJournal,
    default_runner,
)
from omnimarket.nodes.node_install_request_effect.overlay import (
    NODE_NAME,
    ModelInstallRequestEffectOverlay,
    load_overlay,
)
from omnimarket.nodes.node_install_request_owed_compute.handlers.handler_install_request_owed import (
    HandlerInstallRequestOwed,
)
from omnimarket.nodes.node_unit_tree_install_effect.node import (
    NodeUnitTreeInstallEffect,
)

logger = logging.getLogger(__name__)

#: The consumer-group service token, the one lab_work and the focused-run host use.
GROUP_SERVICE = "omnimarket"


class ModelInstallRequestTopics(BaseModel):
    """The node's ``runtime_dispatch`` topics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    success: str
    failure: str


class ModelInstallRequestCommandFailure(BaseModel):
    """What the host publishes on the failure terminal: the handler raised, or
    the command could not be read as a request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "failed"
    request_id: str = ""
    error_message: str = Field(default="", max_length=2000)


class ProtocolBusMessage(Protocol):
    """What the bus hands a subscriber: the record's bytes."""

    @property
    def value(self) -> bytes: ...


def load_install_request_topics(node: str = NODE_NAME) -> ModelInstallRequestTopics:
    """Read the command and terminal topics from the node's contract."""
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("contract.yaml")
        .read_text()
    )
    dispatch = yaml.safe_load(text)["runtime_dispatch"]
    terminals = dispatch["terminal_events"]
    return ModelInstallRequestTopics(
        command=dispatch["command_topic"],
        success=terminals["success"],
        failure=terminals["failure"],
    )


def host_group_id(executor_host: str) -> str:
    """One consumer group per served host: every host sees every command."""
    return (
        f"{derive_service_group_id(NODE_NAME, service=GROUP_SERVICE)}.{executor_host}"
    )


def _envelope_bytes(envelope: ModelEventEnvelope[dict[str, object]]) -> bytes:
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _subscribe_kwargs(bus: ProtocolLabRunBus) -> dict[str, str]:
    """Read retained records from the start when the bus can say so.

    A fresh group on Kafka otherwise starts at the log end, and a request
    retained before the group's partitions were assigned would be missed. The
    in-memory bus keeps no log and takes no such argument.
    """
    parameters = inspect.signature(bus.subscribe).parameters
    return (
        {"auto_offset_reset": "earliest"} if "auto_offset_reset" in parameters else {}
    )


async def _subscribe(
    bus: ProtocolLabRunBus,
    topic: str,
    on_message: Callable[[Any], Awaitable[None]],
    group_id: str,
) -> Callable[[], Awaitable[None]]:
    subscribe: Callable[..., Awaitable[Callable[[], Awaitable[None]]]] = bus.subscribe
    return await subscribe(
        topic, on_message=on_message, group_id=group_id, **_subscribe_kwargs(bus)
    )


class InstallRequestHost:
    """Serves the install request command topic on the host that runs it."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: HandlerInstallRequest,
        overlay: ModelInstallRequestEffectOverlay,
        executor_host: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        *,
        topics: ModelInstallRequestTopics | None = None,
        group_id: str | None = None,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._overlay = overlay
        self._host = executor_host
        self._topics = topics if topics is not None else load_install_request_topics()
        self._group_id = (
            group_id if group_id is not None else host_group_id(executor_host)
        )
        self._now = now
        self._in_flight: set[uuid.UUID] = set()
        #: One install at a time on a host: two installs of one tree would race on its rename.
        self._install_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._held: list[ModelInstallRequest] = []
        self._retry: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], Awaitable[None]] | None = None
        self.processed = 0

    @property
    def topics(self) -> ModelInstallRequestTopics:
        return self._topics

    async def start(self) -> None:
        self._retry = asyncio.create_task(self._retry_held())
        self._unsubscribe = await _subscribe(
            self._bus, self._topics.command, self._on_message, self._group_id
        )
        logger.info(
            "install-request host %s serving %s as group %s",
            self._host,
            self._topics.command,
            self._group_id,
        )

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            await self._unsubscribe()
            self._unsubscribe = None
        tasks = [*self._tasks]
        if self._retry is not None:
            tasks.append(self._retry)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._retry = None

    async def drain(self) -> None:
        """Wait until every request this host took has been answered or held."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def _on_message(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "install-request host %s: unreadable command: %s", self._host, exc
            )
            return
        if not isinstance(raw, dict):
            logger.warning(
                "install-request host %s: command is not an object", self._host
            )
            return
        command_id = _uuid_or_none(raw.get("envelope_id"))
        payload = raw.get("payload", raw)
        try:
            request = ModelInstallRequest.model_validate(payload)
        except ValidationError as exc:
            await self._publish_failure(
                command_id,
                _uuid_or_none(raw.get("correlation_id")),
                str(payload.get("request_id", "")) if isinstance(payload, dict) else "",
                f"the command is not an install request: {exc}",
            )
            return
        task = asyncio.create_task(self._serve(request, command_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(
        self, request: ModelInstallRequest, command_id: uuid.UUID | None
    ) -> None:
        if request.request_id in self._in_flight:
            logger.info(
                "install-request host %s: %s is already being served",
                self._host,
                request.request_id,
            )
            return
        self._in_flight.add(request.request_id)
        try:
            async with self._install_lock:
                handled = await asyncio.to_thread(self._handler.handle, request)
        except Exception as exc:
            # A handler crash is answered on the failure terminal and logged, never swallowed.
            logger.exception(
                "install-request host %s: handler raised for %s",
                self._host,
                request.request_id,
            )
            await self._publish_failure(
                command_id,
                request.request_id,
                str(request.request_id),
                f"{type(exc).__name__}: {exc}",
            )
            return
        finally:
            self._in_flight.discard(request.request_id)
            self.processed += 1
        if handled.result is not None:
            await self._publish_result(request, handled.result)
            return
        if handled.decision.action is EnumInstallRequestAction.HOLD:
            self._held.append(request)
            logger.info(
                "install-request host %s: held %s (%s); deciding again in %ss",
                self._host,
                request.request_id,
                handled.decision.detail,
                self._overlay.hold_retry_seconds,
            )

    async def _retry_held(self) -> None:
        """Decide every held request again after the overlay's retry interval."""
        while True:
            await asyncio.sleep(self._overlay.hold_retry_seconds)
            due, self._held = self._held, []
            for request in due:
                await self._serve(request, None)

    async def _publish_result(
        self, request: ModelInstallRequest, result: ModelInstallRequestResult
    ) -> None:
        await self._bus.publish(
            self._topics.success,
            str(request.request_id).encode("utf-8"),
            envelope_bytes_for(
                self._topics.success, result.model_dump(mode="json"), request.request_id
            ),
        )

    async def _publish_failure(
        self,
        command_id: uuid.UUID | None,
        correlation_id: uuid.UUID | None,
        request_id: str,
        message: str,
    ) -> None:
        failure = ModelInstallRequestCommandFailure(
            request_id=request_id,
            error_message=message[:2000],
        )
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=failure.model_dump(mode="json"),
            correlation_id=correlation_id,
            parent_envelope_id=command_id,
            event_type=event_type_for(self._topics.failure),
        )
        await self._bus.publish(
            self._topics.failure,
            request_id.encode("utf-8") if request_id else None,
            _envelope_bytes(envelope),
        )


async def publish_request(
    bus: ProtocolLabRunBus,
    draft: ModelInstallRequestDraft,
    overlay: ModelInstallRequestEffectOverlay,
    now: Callable[[], datetime],
) -> ModelInstallRequest:
    """Stamp the draft with its id and the overlay's expiry, publish it, return it."""
    stamp = now()
    request = ModelInstallRequest(
        **draft.model_dump(),
        request_id=uuid.uuid4(),
        requested_at=stamp,
        expires_at=stamp + timedelta(minutes=overlay.default_expiry_minutes),
    )
    topics = load_install_request_topics()
    await bus.publish(
        topics.command,
        str(request.request_id).encode("utf-8"),
        envelope_bytes_for(
            topics.command, request.model_dump(mode="json"), request.request_id
        ),
    )
    return request


# ---------------------------------------------------------------------- the CLI


def _add_bus_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bus", choices=("inmemory", "kafka"), required=True, help="the bus to open"
    )
    parser.add_argument("--bus-lane", help="the lane whose broker is used (kafka)")
    parser.add_argument("--kafka-bootstrap", help="an explicit broker address (kafka)")
    parser.add_argument(
        "--omnibase-path",
        default=os.environ.get("OMNIBASE_PATH"),
        help="the workspace root that holds the lane declaration (kafka lane)",
    )


# The opener's keyword for the workspace root, read from its signature rather than spelled here.
_WORKSPACE_KW = next(
    name
    for name in inspect.signature(open_lab_run_bus).parameters
    if name.endswith("_home")
)


def open_install_request_bus(
    *,
    bus: str,
    lane: str | None,
    kafka_bootstrap: str | None,
    workspace_root: Path | None = None,
) -> Any:
    """Open the in-memory bus or a Kafka lane the way every host-side node does.

    A declared Kafka lane needs the workspace root that holds its lane declaration; an explicit broker address needs none.
    """
    return open_lab_run_bus(
        bus=cast(BusKind, bus),
        lane=lane,
        kafka_bootstrap=kafka_bootstrap,
        **{_WORKSPACE_KW: workspace_root},
    )


def _open_bus(args: argparse.Namespace) -> Any:
    return open_install_request_bus(
        bus=args.bus,
        lane=args.bus_lane,
        kafka_bootstrap=args.kafka_bootstrap,
        workspace_root=Path(args.omnibase_path) if args.omnibase_path else None,
    )


async def _serve_command(args: argparse.Namespace) -> None:
    overlay = load_overlay()
    host_name = os.environ.get(overlay.host_name_env, "").strip()
    if not host_name:
        raise ValueError(
            f"{overlay.host_name_env} is not set;"
            " this process would not know the host it serves"
        )
    journal_dir = os.environ.get(overlay.journal_dir_env, "").strip()
    if not journal_dir:
        raise ValueError(
            f"{overlay.journal_dir_env} is not set;"
            " the result journal is relative to it"
        )
    handler = HandlerInstallRequest(
        overlay=overlay,
        executor_host=host_name,
        unit_tree_installer=NodeUnitTreeInstallEffect(),
        runner=default_runner,
        journal=JsonlResultJournal(Path(journal_dir) / overlay.journal_file),
        now=lambda: datetime.now(UTC),
    )
    async with _open_bus(args) as bus:
        host = InstallRequestHost(bus, handler, overlay, host_name)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for each_signal in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(each_signal, stop.set)
        try:
            await host.start()
            await stop.wait()
        finally:
            await host.stop()


async def _publish_command(args: argparse.Namespace) -> None:
    overlay = load_overlay()
    draft = ModelInstallRequestDraft.model_validate_json(sys.stdin.read())
    async with _open_bus(args) as bus:
        request = await publish_request(
            bus, draft, overlay, now=lambda: datetime.now(UTC)
        )
    sys.stdout.write(request.model_dump_json() + "\n")


def _add_owed_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--requests",
        required=True,
        help="the JSON-lines file of published requests (one request per line)",
    )
    parser.add_argument(
        "--results",
        required=True,
        help="the JSON-lines result journal; a missing file reads as no results",
    )
    parser.add_argument(
        "--now", help="the reading time, ISO-8601 with Z, instead of the clock"
    )


def _read_json_lines[ModelT: BaseModel](path: Path, kind: type[ModelT]) -> list[ModelT]:
    """Read one ``kind`` JSON object per line; a missing file holds no records."""
    if not path.exists():
        return []
    records: list[ModelT] = []
    with path.open(encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                records.append(kind.model_validate_json(text))
            except ValidationError as exc:
                raise ValueError(
                    f"{path} line {number} is no {kind.__name__} JSON line: {exc}"
                ) from exc
    return records


def _owed_command(args: argparse.Namespace) -> int:
    requests_path = Path(args.requests)
    if not requests_path.exists():
        raise ValueError(
            f"{requests_path} does not exist: no published requests to read"
        )
    if args.now is not None:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError as exc:
            raise ValueError(f"--now {args.now!r} is no ISO-8601 stamp: {exc}") from exc
    else:
        now = datetime.now(UTC)
    reading = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(
            requests=tuple(_read_json_lines(requests_path, ModelInstallRequest)),
            results=tuple(
                _read_json_lines(Path(args.results), ModelInstallRequestResult)
            ),
            now=now,
        )
    )
    for entry in reading.lines:
        sys.stdout.write(entry.line + "\n")
    sys.stdout.write(f"owed={reading.owed} expired={reading.expired}\n")
    return 1 if reading.expired else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _add_bus_arguments(
        commands.add_parser(
            "serve", help="serve this host's install requests until SIGINT or SIGTERM"
        )
    )
    _add_bus_arguments(
        commands.add_parser(
            "publish",
            help="read one draft JSON on stdin, publish it, print the request",
        )
    )
    _add_owed_arguments(
        commands.add_parser(
            "owed",
            help="read the result journal, print the installs still owed",
        )
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "serve":
            asyncio.run(_serve_command(args))
        elif args.command == "publish":
            asyncio.run(_publish_command(args))
        else:
            return _owed_command(args)
    except LabRunBusError as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
