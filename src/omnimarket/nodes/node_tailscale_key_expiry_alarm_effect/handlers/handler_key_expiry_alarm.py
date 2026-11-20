# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerKeyExpiryAlarm is the pure expiry judge that HandlerKeyExpiryTick in this module constructs directly; the runtime loads HandlerKeyExpiryTick from the node contract handler block, not from a Python import
"""Judge expiry dates and record one alarm per observed key-expiry episode."""

from __future__ import annotations

import fcntl
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import (
    ProtocolAlarmChannel,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.adapters.tailnet_status import (
    read_status,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.models import (
    ModelKeyExpiryAlarm,
    ModelKeyExpiryCache,
    ModelKeyExpiryRequest,
    ModelKeyExpiryResult,
    ModelKeyExpiryTickRequest,
    ModelKeyExpiryTickResult,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.overlay import (
    ModelKeyExpiryOverlay,
)

_WARNING = timedelta(days=14)
_MAX_OBSERVATION_AGE = timedelta(hours=48)
_CONDITION = "tailscale-key-expiry"


class HandlerKeyExpiryAlarm:
    def handle(self, request: ModelKeyExpiryRequest) -> ModelKeyExpiryResult:
        cutoff = request.now + _WARNING
        return ModelKeyExpiryResult(
            alarms=tuple(
                ModelKeyExpiryAlarm(
                    subject=host,
                    owner=request.owner,
                    raised_at=request.now,
                    observed_at=reading.observed_at,
                    expires_at=reading.expires_at,
                )
                for host, reading in sorted(request.readings.items())
                if reading.expired
                or (reading.expires_at is not None and reading.expires_at <= cutoff)
            )
        )


def _write_json(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(value.model_dump_json() + "\n", encoding="utf-8")
    temp.replace(path)


def _alarm_text(row: ModelKeyExpiryAlarm) -> str:
    expiry = row.expires_at.isoformat() if row.expires_at else "reported expired"
    return (
        f"ALARM {row.condition} | host={row.subject} | owner={row.owner} | "
        f"expiry={expiry} | observed_at={row.observed_at.isoformat()}"
    )


class HandlerKeyExpiryTick:
    """Run one tick from the unit, serializing writes across manual and scheduled runs."""

    def __init__(
        self,
        *,
        overlay: ModelKeyExpiryOverlay,
        aliases: Mapping[str, tuple[str, ...]],
        state_dir: Path,
        read_status_document: Callable[[], Mapping[str, object] | None],
        channel: ProtocolAlarmChannel,
    ) -> None:
        self._overlay = overlay
        self._aliases = aliases
        self._state_dir = state_dir
        self._read_status_document = read_status_document
        self._channel = channel

    def handle(self, request: ModelKeyExpiryTickRequest) -> ModelKeyExpiryTickResult:
        if request.probe:
            return self._tick(request)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        with (self._state_dir / "tick.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self._tick(request)
            _write_json(self._state_dir / "last-run.json", result)
            return result

    def _tick(self, request: ModelKeyExpiryTickRequest) -> ModelKeyExpiryTickResult:
        now = request.now
        live = read_status(
            self._read_status_document(),
            self._aliases,
            now,
            watching_host=self._overlay.watching_host,
        )
        cache_path = self._state_dir / "status-cache.json"
        try:
            cache = ModelKeyExpiryCache.model_validate_json(cache_path.read_bytes())
        except FileNotFoundError:
            cache = ModelKeyExpiryCache(readings={})
        watched = set(self._aliases)
        if (
            self._overlay.watching_host in (live or {})
            or self._overlay.watching_host in cache.readings
        ):
            watched.add(self._overlay.watching_host)
        readings = {
            host: row
            for host, row in cache.readings.items()
            if host in watched
            and timedelta(0) <= now - row.observed_at <= _MAX_OBSERVATION_AGE
        }
        cached = readings.keys() - (live or {}).keys()
        readings.update(live or {})
        if live is not None and not request.probe:
            _write_json(cache_path, ModelKeyExpiryCache(readings=readings))
        reported = {
            host
            for host, row in readings.items()
            if row.expired or row.expires_at is not None
        }
        source: Literal["live", "cache", "unavailable"] = "live"
        if live is None:
            source = "cache" if reported else "unavailable"
        verdict = HandlerKeyExpiryAlarm().handle(
            ModelKeyExpiryRequest(now=now, owner=self._overlay.owner, readings=readings)
        )
        raised = delivered = 0
        refused: list[str] = []
        if not request.probe:
            for alarm in verdict.alarms:
                new, pushed = self._deliver(alarm)
                raised += new
                if pushed is None:
                    refused.append(alarm.subject)
                else:
                    delivered += pushed
        return ModelKeyExpiryTickResult(
            ran_at=now,
            source=source,
            cached_hosts=tuple(sorted(cached & reported)),
            unread_hosts=tuple(sorted(watched - reported)),
            due=len(verdict.alarms),
            raised=raised,
            delivered=delivered,
            push_refused=tuple(refused),
        )

    def _deliver(self, alarm: ModelKeyExpiryAlarm) -> tuple[bool, bool | None]:
        episode = uuid5(
            NAMESPACE_URL, f"{_CONDITION}/{alarm.subject}/{alarm.expires_at}"
        )
        path = self._state_dir / "alarms" / f"{episode}.json"
        try:
            row = ModelKeyExpiryAlarm.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            row = alarm
            _write_json(path, row)
            new = True
        else:
            new = False
        if row.message_ref is not None:
            return new, False
        receipt = self._channel.send(category=_CONDITION, text=_alarm_text(row))
        if not receipt.delivered:
            return new, None
        _write_json(path, row.model_copy(update={"message_ref": receipt.message_ref}))
        return new, True
