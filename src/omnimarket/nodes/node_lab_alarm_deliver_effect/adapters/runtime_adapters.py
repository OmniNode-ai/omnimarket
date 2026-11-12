# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime backend: the alert-channel library, the ledger host's bus append and a state file."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omnimarket.events.alarm_send_receipt import ModelAlarmSendReceipt
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.alert_channel_sender import (
    AlertChannelAlarmSender,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import LedgerAppendError
from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.adapters import (
    BusLaneLedgerAppender,
)


class ChannelFromLibrary:
    """Posts through the alert-channel library's ``alert_channel_alarm``, the path the liveness watchdog sends over."""

    def __init__(self, library: Path, timeout_seconds: float) -> None:
        self._sender = AlertChannelAlarmSender(library, timeout_seconds=timeout_seconds)

    def send(self, *, category: str, text: str) -> ModelAlarmSendReceipt:
        return self._sender.send(episode_id=uuid4(), category=category, text=text)


class BusLedger:
    """Appends over the ledger host's bus on the declared lane, the path the pin-friction effect writes over."""

    def __init__(
        self,
        bus_lane: str,
        workspace_root: Path,
        lane_name: str,
        timeout_seconds: float,
        host: str,
    ) -> None:
        self._appender = BusLaneLedgerAppender(bus_lane, workspace_root)
        self._lane_name = lane_name
        self._timeout = timeout_seconds
        self._host = host

    def append(self, *, request_id: str, row: str) -> str:
        request = ModelWorkLedgerAppendRequest(
            request_id=UUID(request_id),
            rows=row + "\n",
            requested_by_lane=self._lane_name,
            requesting_host=self._host,
            requested_at=datetime.now(UTC),
        )
        try:
            receipt = asyncio.run(
                self._appender.append(request, timeout_s=self._timeout)
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise LedgerAppendError(f"{type(exc).__name__}: {exc}") from exc
        if receipt is None:
            raise LedgerAppendError(
                f"no receipt within {self._timeout:g}s; the next request resends under the same id"
            )
        if receipt.request_id != request.request_id:
            raise LedgerAppendError(
                f"the ledger answered for request {receipt.request_id}"
            )
        if receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED:
            return "accepted"
        if receipt.status in (
            EnumWorkLedgerAppendStatus.DUPLICATE,
            EnumWorkLedgerAppendStatus.ALREADY_PRESENT,
        ):
            return "duplicate"
        raise LedgerAppendError(f"ledger {receipt.status.value}: {receipt.message}")


class FileDeliveryState:
    """The episode state in one JSON file; a corrupt file is refused, never read as empty."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"delivery state {self._path} is unreadable: {type(exc).__name__}"
            ) from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"delivery state {self._path} is not an object")
        return loaded

    def save(self, state: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(self._path.name + ".tmp")
        temp.write_text(json.dumps(dict(state), sort_keys=True), encoding="utf-8")
        os.replace(temp, self._path)


__all__ = ["BusLedger", "ChannelFromLibrary", "FileDeliveryState"]
