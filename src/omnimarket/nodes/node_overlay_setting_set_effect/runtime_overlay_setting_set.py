# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_setting_set_effect``: set one declared key of one overlay in the store."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.handlers.overlay_store.store_bus_snapshot import BusSnapshotOverlayStore
from omnimarket.models.overlay_store import (
    ModelOverlayStoreLoadOverlay,
    ModelOverlayWriteReply,
)
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    HandlerOverlaySettingSet,
    ProtocolPendingReply,
    SettingReplyTimeoutError,
    load_settable_settings,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_result import (
    ModelOverlaySettingSetResult,
)

NODE = "node_overlay_setting_set_effect"
# The setter writes the store the loader writes, so it takes the loader's bus declaration (its overlay).
LOADER_NODE = "node_overlay_store_load_effect"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


class _Pending:
    def __init__(self, future: asyncio.Future[ModelOverlayWriteReply]) -> None:
        self._future = future

    async def result(self, timeout_s: float) -> ModelOverlayWriteReply:
        # Shielded: a timeout leaves the future for the same write id published again.
        return await asyncio.wait_for(asyncio.shield(self._future), timeout_s)


class _Replies:
    """The store's replies by write id: the listener resolves the future the set registered before it published."""

    def __init__(self) -> None:
        self._waiting: dict[str, asyncio.Future[ModelOverlayWriteReply]] = {}
        self.malformed = 0

    @asynccontextmanager
    async def expect(self, write_id: str) -> AsyncIterator[ProtocolPendingReply]:
        future: asyncio.Future[ModelOverlayWriteReply] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting[write_id] = future
        try:
            yield _Pending(future)
        finally:
            self._waiting.pop(write_id, None)

    async def on_message(self, message: Any) -> None:
        try:
            reply = ModelOverlayWriteReply.model_validate_json(bytes(message.value))
        except ValueError:
            self.malformed += 1
            return
        waiting = self._waiting.get(str(reply.write_id))
        if waiting is not None and not waiting.done():
            waiting.set_result(reply)


class _WriteBus:
    """The bus the set publishes on and the store's reply comes back on, opened at the first publish.

    The handler reads the live record before it writes. A bus held open for the whole run made that read open a second
    lane bus beside it; opening at the first publish means the read has closed its own bus by then, so the command
    holds one lane bus at a time. The reply listener rides this bus and is subscribed before the first publish.
    """

    def __init__(
        self,
        open_bus: Callable[[], Any],
        stack: AsyncExitStack,
        replies: _Replies,
        reply_topic: str,
        reply_group_prefix: str,
    ) -> None:
        self._open_bus = open_bus
        self._stack = stack
        self._replies = replies
        self._reply_topic = reply_topic
        self._reply_group_prefix = reply_group_prefix
        self._bus: Any = None

    async def _open(self) -> Any:
        bus = await self._stack.enter_async_context(self._open_bus())
        # A broker replays from the earliest retained offset, so a reply sent before the group is assigned is read.
        extra = (
            {"auto_offset_reset": "earliest"}
            if "auto_offset_reset" in inspect.signature(bus.subscribe).parameters
            else {}
        )
        stop = await bus.subscribe(
            self._reply_topic,
            on_message=self._replies.on_message,
            group_id=f"{self._reply_group_prefix}.{uuid.uuid4().hex}",
            **extra,
        )
        self._stack.push_async_callback(stop)
        return bus

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        if self._bus is None:
            self._bus = await self._open()
        if headers is None:
            return await self._bus.publish(topic, key, value)
        return await self._bus.publish(topic, key, value, headers)


async def _run(
    request: ModelOverlaySettingSetRequest,
    overlay: ModelOverlayStoreLoadOverlay,
    kafka_bootstrap: str | None,
    read_window_s: float | None,
    max_live_age_s: float | None = None,
) -> ModelOverlaySettingSetResult:
    contract = _contract()
    bootstrap = kafka_bootstrap or overlay.kafka_bootstrap

    def open_bus() -> Any:
        return open_lane_bus(
            bus=overlay.bus,
            lane=None if bootstrap or overlay.bus == "inmemory" else overlay.bus_lane,
            kafka_bootstrap=bootstrap,
            root=workspace_root(os.environ),
        )

    reader = BusSnapshotOverlayStore(
        open_bus=open_bus,
        topic=contract["snapshot_read"]["topic"],
        group_prefix=contract["config"]["group_prefix"],
        window_s=read_window_s or float(contract["config"]["read_window_s"]),
    )
    config = contract["config"]
    replies = _Replies()
    async with AsyncExitStack() as stack:
        handler = HandlerOverlaySettingSet(
            reader=reader,
            bus=_WriteBus(
                open_bus,
                stack,
                replies,
                contract["reply_read"]["topic"],
                config["reply_group_prefix"],
            ),
            replies=replies,
            settings=load_settable_settings(contract),
            loaded_topic=contract["event_bus"]["publish_topics"][0],
            audit_topic=contract["externally_consumed_topics"][0],
            max_live_age_s=max_live_age_s
            if max_live_age_s is not None
            else float(config["max_live_age_s"]),
            reply_timeout_s=float(config["reply_timeout_s"]),
            max_attempts=int(config["max_attempts"]),
        )
        return await handler.handle(request)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE, description=__doc__)
    parser.add_argument("--overlay", required=True, help="the overlay id")
    parser.add_argument("--key", required=True, help="a key the contract declares")
    parser.add_argument(
        "--actor", required=True, help="who sets it: the audit row names it"
    )
    value = parser.add_mutually_exclusive_group(required=True)
    value.add_argument("--value-json", help="the new value, as JSON")
    value.add_argument(
        "--value-file", type=Path, help="a file holding the new value as JSON"
    )
    parser.add_argument("--kafka-bootstrap", default=None)
    parser.add_argument("--read-window-s", type=float, default=None)
    parser.add_argument(
        "--max-live-age-s",
        type=float,
        default=None,
        help="the oldest live record this set accepts as its base, in seconds; default is the contract's bound. "
        "A row set into the store alone is asserted by no loader, so it ages past the bound; naming the age "
        "re-asserts it, and the audit row still names who set it and the previous and new value",
    )
    args = parser.parse_args(argv)
    try:
        text = (
            args.value_json
            if args.value_json is not None
            else args.value_file.read_text(encoding="utf-8")
        )
        request = ModelOverlaySettingSetRequest(
            overlay_id=args.overlay,
            key=args.key,
            value=json.loads(text),
            actor=args.actor,
        )
        overlay = load_structured_overlay(
            LOADER_NODE, ModelOverlayStoreLoadOverlay, os.environ
        )
        result = asyncio.run(
            _run(
                request,
                overlay,
                args.kafka_bootstrap,
                args.read_window_s,
                args.max_live_age_s,
            )
        )
    except SettingReplyTimeoutError as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return 70
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return 65
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


__all__ = ["main"]
