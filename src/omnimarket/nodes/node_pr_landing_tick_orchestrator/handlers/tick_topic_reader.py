# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick orchestrator's real topic reader (OMN-23458).

It opens the overlay's declared bus lane (the broker and client transport resolve from the lane
config under workspace_root, as the snapshot step's publisher does), or a plaintext lab broker for
a proof, subscribes to the snapshot topic under the overlay's consumer group from the earliest
uncommitted record, and returns each record's JSON.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickTopicSelection,
)
from omnimarket.topic_namespace import apply_topic_namespace


class TickJoinTimeoutError(Exception):
    """The consumer group join took longer than the overlay's join_timeout_s: the pass ends unread."""


class BusTickTopicReader:
    """ProtocolTickTopicReader over the overlay's declared bus lane or lab broker (OMN-23458)."""

    def __init__(self, selection: ModelTickTopicSelection) -> None:
        self._selection = selection

    def read(self, max_records: int, timeout_s: float) -> list[str]:
        return asyncio.run(self._read(max_records, timeout_s))

    async def _read(self, max_records: int, timeout_s: float) -> list[str]:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        selection = self._selection
        root = (
            Path(os.path.expanduser(os.path.expandvars(selection.workspace_root)))
            if selection.workspace_root
            else None
        )
        async with open_lane_bus(
            bus="kafka",
            lane=selection.bus_lane,
            kafka_bootstrap=None if selection.bus_lane else selection.bootstrap_servers,
            root=root,
        ) as bus:
            raw: list[str] = []
            done = asyncio.Event()

            async def collect(message: Any) -> None:
                if len(raw) < max_records:
                    raw.append(bytes(message.value).decode())
                if len(raw) >= max_records:
                    done.set()

            try:
                handle = await asyncio.wait_for(
                    bus.subscribe(
                        apply_topic_namespace(selection.topic),
                        on_message=collect,
                        group_id=selection.consumer_group,
                        auto_offset_reset="earliest",
                    ),
                    selection.join_timeout_s,
                )
            except TimeoutError:
                raise TickJoinTimeoutError(
                    f"the consumer group {selection.consumer_group} did not join "
                    f"within {selection.join_timeout_s} s"
                ) from None
            try:
                await asyncio.wait_for(done.wait(), timeout_s)
            except TimeoutError:
                pass
            finally:
                await handle.stop()
        return raw


__all__: list[str] = ["BusTickTopicReader", "TickJoinTimeoutError"]
