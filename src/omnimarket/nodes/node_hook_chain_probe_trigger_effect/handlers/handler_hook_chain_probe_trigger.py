# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request a hook-chain probe on the runtime heartbeat, once per interval.

Canonical definition-B shape: typed payload in, typed payload (or ``None`` to
suppress publication) out. It owns no timer, loop or thread; the cadence is
``probe_schedule.probe_interval_seconds`` in this node's contract, carried by the
heartbeat the runtime already emits, so the schedule dies with the runtime.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from omnimarket.nodes.node_hook_chain_probe_trigger_effect.models.model_hook_chain_probe_trigger import (
    ModelHookChainProbeHeartbeat,
    ModelHookChainProbeScheduleRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"


def _declared_interval(contract_path: Path) -> int:
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    interval = raw["probe_schedule"]["probe_interval_seconds"]
    if not isinstance(interval, int) or interval <= 0:
        raise ValueError(
            f"{contract_path}: probe_schedule.probe_interval_seconds must be a "
            "positive integer"
        )
    return interval


class HandlerHookChainProbeTrigger:
    """Emit one probe command per wall-clock interval."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        interval_seconds: int | None = None,
    ) -> None:
        self._clock = clock or time.time
        self.interval_seconds = (
            interval_seconds
            if interval_seconds is not None
            else _declared_interval(_CONTRACT_PATH)
        )
        # The only state: a throttle bucket, not a verdict. Losing it on restart
        # probes one interval early, which is harmless.
        self._last_bucket: int | None = None

    def handle(
        self, request: ModelHookChainProbeHeartbeat
    ) -> ModelHookChainProbeScheduleRequest | None:
        """Return the probe command when the interval is due, else ``None``."""
        bucket = int(self._clock()) // self.interval_seconds
        if self._last_bucket is not None and bucket <= self._last_bucket:
            return None
        self._last_bucket = bucket
        return ModelHookChainProbeScheduleRequest()


__all__ = ["HandlerHookChainProbeTrigger"]
