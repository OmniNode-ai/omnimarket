# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_setting_set_effect``: set one declared key of one overlay in the store."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
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
from omnimarket.models.overlay_store import ModelOverlayStoreLoadOverlay
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    HandlerOverlaySettingSet,
    SettingAuditFailedError,
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


class _WriteBus:
    """The bus the set publishes on, opened at the first publish.

    The handler reads the live record before it writes. A bus held open for the whole run made that read open a second
    lane bus beside it; opening at the first publish means the read has closed its own bus by then, so the command
    holds one lane bus at a time.
    """

    def __init__(self, open_bus: Callable[[], Any], stack: AsyncExitStack) -> None:
        self._open_bus = open_bus
        self._stack = stack
        self._bus: Any = None

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        if self._bus is None:
            self._bus = await self._stack.enter_async_context(self._open_bus())
        if headers is None:
            return await self._bus.publish(topic, key, value)
        return await self._bus.publish(topic, key, value, headers)


async def _run(
    request: ModelOverlaySettingSetRequest,
    overlay: ModelOverlayStoreLoadOverlay,
    kafka_bootstrap: str | None,
    read_window_s: float | None,
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
    topics = contract["event_bus"]["publish_topics"]
    async with AsyncExitStack() as stack:
        handler = HandlerOverlaySettingSet(
            reader=reader,
            bus=_WriteBus(open_bus, stack),
            settings=load_settable_settings(contract),
            loaded_topic=topics[0],
            audit_topic=topics[1],
            max_live_age_s=float(contract["config"]["max_live_age_s"]),
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
            _run(request, overlay, args.kafka_bootstrap, args.read_window_s)
        )
    except SettingAuditFailedError as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return 70
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return 65
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


__all__ = ["main"]
