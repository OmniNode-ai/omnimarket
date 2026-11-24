# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Valkey overlay store: each overlay's current record, revision and heartbeat under the key prefix the wiring gives.

It holds no address, port, database or prefix of its own (the client and the prefix arrive in the constructor). A record
is applied only when its revision is newer than the one held, or the same revision with a later heartbeat, so a replay of
the topic never moves a value back. A record that carries no revision (published before revisions existed) is never
applied and is counted.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Mapping
from datetime import datetime
from typing import Any, NamedTuple, Protocol

from omnibase_core.types import JsonType

from omnimarket.models.overlay_store.model_overlay_settings_record import (
    ModelOverlaySettingsRecord,
)


class ProtocolValkeyClient(Protocol):
    """What the store needs of an asyncio redis client, or a double."""

    def get(self, name: str, /) -> Awaitable[str | bytes | None]: ...

    def set(self, name: str, value: str, /) -> Awaitable[object]: ...


class ValkeyStoreReading(NamedTuple):
    """What the writer did since it started: the records applied, and the pre-revision records it skipped."""

    applied: int
    skipped_pre_revision: int


class ValkeyOverlayStore:
    """``ProtocolOverlayStore`` over a Valkey client, one JSON value per overlay under ``<key_prefix>:<overlay_id>``."""

    def __init__(self, client: ProtocolValkeyClient, *, key_prefix: str) -> None:
        if not key_prefix:
            raise ValueError("the key prefix comes from the wiring and is not empty")
        self._client = client
        self._prefix = key_prefix
        self._applied = 0
        self._skipped = 0

    def reading(self) -> ValkeyStoreReading:
        return ValkeyStoreReading(self._applied, self._skipped)

    def _key(self, overlay_id: str) -> str:
        return f"{self._prefix}:{overlay_id}"

    async def read_current(self, overlay_id: str) -> Mapping[str, JsonType] | None:
        raw = await self._client.get(self._key(overlay_id))
        if raw is None:
            return None
        text = raw if isinstance(raw, str) else raw.decode("utf-8")
        held: dict[str, JsonType] = json.loads(text)
        return held

    async def write_version(self, record: Mapping[str, JsonType]) -> bool:
        incoming = ModelOverlaySettingsRecord.model_validate(record)
        if incoming.revision is None:
            self._skipped += 1
            return False
        held = await self.read_current(incoming.overlay_id)
        if held is not None and not _supersedes(incoming, held):
            return False
        await self._client.set(
            self._key(incoming.overlay_id), json.dumps(dict(record), sort_keys=True)
        )
        self._applied += 1
        return True


def _supersedes(incoming: ModelOverlaySettingsRecord, held: Mapping[str, Any]) -> bool:
    held_revision = held.get("revision")
    if not isinstance(held_revision, int):
        return True
    assert incoming.revision is not None
    if incoming.revision != held_revision:
        return incoming.revision > held_revision
    held_beat = held.get("heartbeat_at")
    if incoming.heartbeat_at is None:
        return False
    if held_beat is None:
        return True
    return incoming.heartbeat_at > datetime.fromisoformat(str(held_beat))


__all__ = ["ProtocolValkeyClient", "ValkeyOverlayStore", "ValkeyStoreReading"]
