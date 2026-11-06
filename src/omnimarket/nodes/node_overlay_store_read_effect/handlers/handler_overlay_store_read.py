# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: read one loaded overlay, keep this host's snapshot, say how the read went.

Placement must work when the store or the broker is down, so readers take the local snapshot and a
refresh keeps it. A refresh that cannot reach the store, or finds nothing or something invalid, leaves
the snapshot as it is. Past an overlay's bound the snapshot reads per that overlay's policy: ``read``
answers and every reading says stale, so a stale table is never silently refused and never silently
trusted; ``refuse`` raises. No usable snapshot reads as unreadable, with its reason.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue, ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_store.snapshot_root import (
    FileOverlaySnapshot,
    OverlaySnapshotError,
)
from omnimarket.models.overlay_store import (
    EnumOverlayReadSource,
    EnumOverlayReadState,
    ModelOverlayCompare,
    ModelOverlayDocumentRead,
    ModelOverlayReadPolicy,
    ModelOverlaySnapshotStamp,
    ModelOverlayStoreReading,
    ModelOverlayStoreRecord,
)

if TYPE_CHECKING:
    from omnibase_spi.protocols.storage import ProtocolOverlayStore


class OverlayAbsentError(NodeOverlayError):
    """No usable snapshot of the overlay on this host."""


class OverlayStaleError(NodeOverlayError):
    """The overlay's policy refuses a read past its bound."""


class ProtocolOverlayReporter(Protocol):
    """Where a reading goes. A reporter that fails never fails the read."""

    async def report(self, reading: ModelOverlayStoreReading) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerOverlayStoreRead:
    """The refresh and the read, with every dependency injected."""

    def __init__(
        self,
        *,
        store: ProtocolOverlayStore,
        snapshot: FileOverlaySnapshot,
        reporter: ProtocolOverlayReporter,
        host: str,
        policies: Mapping[str, ModelOverlayReadPolicy],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._snapshot = snapshot
        self._reporter = reporter
        self._host = host
        self._policies = policies
        self._clock = clock

    async def handle(self, overlay_id: str) -> ModelOverlayStoreReading:
        """One refresh of one overlay: the store's version into the snapshot, or why it stayed."""
        now = self._clock()
        store_error: str | None = None
        try:
            record = await self._store.read_current(overlay_id)
            if record is None:
                store_error = f"the store holds no overlay {overlay_id}"
            else:
                loaded = ModelOverlayStoreRecord.model_validate(record)
                if loaded.overlay_id != overlay_id:
                    raise ValueError(
                        f"asked for {overlay_id}, the store answered {loaded.overlay_id}"
                    )
                stamp = self._snapshot.write(loaded, written_at=now)
                reading = self._reading(
                    overlay_id, stamp, EnumOverlayReadSource.STORE, None, now=now
                )
                await self._report(reading)
                return reading
        except ValidationError as exc:
            store_error = f"the store's overlay is invalid: {exc.errors()[0]['msg']}"
        except (
            Exception
        ) as exc:  # any backend failure is one fact: the store did not answer
            store_error = f"{type(exc).__name__}: {exc}"
        reading = self.read(overlay_id, store_error=store_error)
        await self._report(reading)
        return reading

    async def refresh_all(self) -> tuple[ModelOverlayStoreReading, ...]:
        """One refresh of every overlay this host keeps."""
        return tuple([await self.handle(overlay_id) for overlay_id in self._policies])

    def read(
        self, overlay_id: str, *, store_error: str | None = None
    ) -> ModelOverlayStoreReading:
        """The snapshot as it stands, with its age against the bound. Touches no store."""
        now = self._clock()
        try:
            stamp = self._snapshot.read_stamp(overlay_id)
        except OverlaySnapshotError as exc:
            return ModelOverlayStoreReading(
                host=self._host,
                overlay_id=overlay_id,
                state=EnumOverlayReadState.UNREADABLE,
                stale_after_s=self._policy(overlay_id).stale_after_s,
                store_error=store_error,
                detail=str(exc),
                read_at=now,
            )
        return self._reading(
            overlay_id, stamp, EnumOverlayReadSource.SNAPSHOT, store_error, now=now
        )

    def read_document(self, overlay_id: str) -> ModelOverlayDocumentRead:
        """The document with its reading; absent raises, and stale raises where the policy refuses."""
        reading = self.read(overlay_id)
        if reading.state is EnumOverlayReadState.UNREADABLE:
            raise OverlayAbsentError(f"overlay {overlay_id}: {reading.detail}")
        if (
            reading.state is EnumOverlayReadState.STALE
            and self._policy(overlay_id).on_stale == "refuse"
        ):
            raise OverlayStaleError(f"overlay {overlay_id}: {reading.detail}")
        return ModelOverlayDocumentRead(
            document=self._snapshot.read_document(overlay_id), reading=reading
        )

    def _policy(self, overlay_id: str) -> ModelOverlayReadPolicy:
        policy = self._policies.get(overlay_id)
        if policy is None:
            raise NodeOverlayError(
                f"overlay {overlay_id}: this host keeps {sorted(self._policies)}"
            )
        return policy

    def _reading(
        self,
        overlay_id: str,
        stamp: ModelOverlaySnapshotStamp,
        source: EnumOverlayReadSource,
        store_error: str | None,
        *,
        now: datetime,
    ) -> ModelOverlayStoreReading:
        bound = self._policy(overlay_id).stale_after_s
        age = (now - stamp.loaded_at).total_seconds()
        stale = age > bound
        return ModelOverlayStoreReading(
            host=self._host,
            overlay_id=overlay_id,
            state=EnumOverlayReadState.STALE if stale else EnumOverlayReadState.FRESH,
            source=source,
            digest=stamp.digest,
            age_s=round(age, 1),
            stale_after_s=bound,
            store_error=store_error,
            detail=(
                f"source last asserted {age:.0f}s ago, over the {bound}s bound"
                if stale
                else ""
            ),
            read_at=now,
        )

    async def _report(self, reading: ModelOverlayStoreReading) -> None:
        try:
            await self._reporter.report(reading)
        except (
            Exception
        ) as exc:  # the read stands; a lost report is printed, never raised
            sys.stderr.write(
                f"overlay reading not reported: {type(exc).__name__}: {exc}\n"
            )


def _row_key(row: object) -> str | None:
    return (
        str(row["name"])
        if isinstance(row, Mapping) and isinstance(row.get("name"), str)
        else None
    )


def _walk(
    left: JsonValue,
    right: JsonValue,
    path: str,
    differing: list[str],
    counts: list[int],
) -> None:
    counts[0] += 1
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        for key in sorted(set(left) | set(right)):
            where = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                differing.append(where)
            else:
                _walk(left[key], right[key], where, differing, counts)
        return
    if isinstance(left, list) and isinstance(right, list):
        keys_left = [_row_key(item) for item in left]
        keys_right = [_row_key(item) for item in right]
        if all(keys_left) and all(keys_right) and left and right:
            counts[1] += len(left)
            if keys_left != keys_right:
                differing.append(f"{path}.<row order or names>")
            by_name = dict(zip(keys_right, right, strict=True))
            for name, row in zip(keys_left, left, strict=True):
                if name in by_name:
                    _walk(row, by_name[name], f"{path}.{name}", differing, counts)
            return
        if len(left) != len(right):
            differing.append(f"{path}.<length>")
            return
        for index, (one, other) in enumerate(zip(left, right, strict=True)):
            _walk(one, other, f"{path}[{index}]", differing, counts)
        return
    if left != right or type(left) is not type(right):
        differing.append(path)


def compare_documents(
    source: Mapping[str, JsonValue], stored: Mapping[str, JsonValue]
) -> ModelOverlayCompare:
    """Column by column, with a list of named rows compared row by row and by name."""
    differing: list[str] = []
    counts = [0, 0]
    _walk(dict(source), dict(stored), "", differing, counts)
    return ModelOverlayCompare(
        equal=not differing,
        paths_compared=counts[0],
        rows_compared=counts[1],
        differing=tuple(differing),
    )


__all__ = [
    "HandlerOverlayStoreRead",
    "OverlayAbsentError",
    "OverlayStaleError",
    "ProtocolOverlayReporter",
    "compare_documents",
]
