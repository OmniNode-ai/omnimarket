# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory overlay store: the default when no overlay selects a backend.

It keeps the rules of the Postgres store (``store_postgres``): a load is applied when it is newer than the record held and
is numbered when it changes the document; a set is applied only when the revision it was built on is the current one,
and its record, its provenance and its audit row are committed together or not at all; a write id is applied once.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from omnibase_core.types import JsonType

from omnimarket.models.overlay_store import (
    EnumOverlayValueSource,
    EnumOverlayWriteOutcome,
    ModelOverlayPathProvenance,
    ModelOverlaySettingAudit,
    ModelOverlayStoreRecord,
    ModelOverlayStoreWrite,
    ModelOverlayWriteReply,
)

_WRITE_ONLY_FIELDS = {"write_id", "based_on_revision", "audit"}


class InMemoryOverlayStore:
    """Holds one record per overlay id; a redelivered or older load changes nothing."""

    def __init__(self) -> None:
        self._records: dict[str, Mapping[str, JsonType]] = {}
        self._provenance: dict[str, dict[str, ModelOverlayPathProvenance]] = {}
        self._writes: dict[str, ModelOverlayWriteReply] = {}
        self._audits: dict[str, ModelOverlaySettingAudit] = {}
        self._published: set[str] = set()

    async def write_version(self, record: Mapping[str, JsonType]) -> bool:
        overlay_id = str(record["overlay_id"])
        current = self._records.get(overlay_id)
        if current is not None and _loaded_at(current) >= _loaded_at(record):
            return False
        self._records[overlay_id] = dict(record)
        return True

    async def read_current(self, overlay_id: str) -> Mapping[str, JsonType] | None:
        return self._records.get(overlay_id)

    async def read_provenance(
        self, overlay_id: str
    ) -> dict[str, ModelOverlayPathProvenance]:
        return dict(self._provenance.get(overlay_id, {}))

    @property
    def audit_write_ids(self) -> tuple[str, ...]:
        """The write ids that hold an audit row, in the order they were committed."""
        return tuple(self._audits)

    async def pending_audit(self, write_id: str) -> ModelOverlaySettingAudit | None:
        """The audit row of the write when it has not been published yet."""
        if write_id in self._published:
            return None
        return self._audits.get(write_id)

    async def mark_audit_published(self, write_id: str) -> None:
        if write_id in self._audits:
            self._published.add(write_id)

    async def apply_write(
        self, write: ModelOverlayStoreWrite
    ) -> ModelOverlayWriteReply:
        """Apply one load or one set. Nothing here awaits, so one call is atomic among the tasks of the loop."""
        if write.audit is None:
            return self._apply_load(write)
        return self._apply_set(write, write.audit)

    def _revision(self, overlay_id: str) -> int:
        current = self._records.get(overlay_id)
        return int(str(current.get("revision", 0))) if current is not None else 0

    def _held(self, record: ModelOverlayStoreRecord) -> dict[str, JsonType]:
        held: dict[str, JsonType] = record.model_dump(
            mode="json", exclude=_WRITE_ONLY_FIELDS
        )
        return held

    def _apply_load(self, write: ModelOverlayStoreWrite) -> ModelOverlayWriteReply:
        overlay_id = write.overlay_id
        current = self._records.get(overlay_id)
        revision = self._revision(overlay_id)
        if current is not None and _loaded_at(current) >= write.loaded_at:
            return ModelOverlayWriteReply(
                overlay_id=overlay_id,
                outcome=EnumOverlayWriteOutcome.COMMITTED,
                revision=revision,
                replayed=True,
            )
        if current is None or current.get("digest") != write.digest:
            revision += 1
        # A loader's load names no operator path: the paths the store holds stay marked.
        paths = _operator_paths(current)
        record = write.model_copy(
            update={"revision": revision, "operator_paths": paths}
        )
        self._records[overlay_id] = self._held(record)
        return ModelOverlayWriteReply(
            overlay_id=overlay_id,
            outcome=EnumOverlayWriteOutcome.COMMITTED,
            revision=revision,
        )

    def _apply_set(
        self, write: ModelOverlayStoreWrite, audit: ModelOverlaySettingAudit
    ) -> ModelOverlayWriteReply:
        overlay_id = write.overlay_id
        write_id = str(write.write_id)
        first = self._writes.get(write_id)
        if first is not None:
            return first.model_copy(update={"replayed": True})
        revision = self._revision(overlay_id)
        if write.based_on_revision != revision:
            return ModelOverlayWriteReply(
                write_id=write_id,
                overlay_id=overlay_id,
                outcome=EnumOverlayWriteOutcome.CONFLICT,
                revision=revision,
            )
        new_revision = revision + 1
        current = self._records.get(overlay_id)
        paths = sorted({*_operator_paths(current), *write.operator_paths})
        # Everything below is built aside and committed last, so a failure leaves the store as it was.
        audits = dict(self._audits)
        self._write_audit(audits, write_id, audit)
        provenance = dict(self._provenance.get(overlay_id, {}))
        provenance[audit.setting_path] = ModelOverlayPathProvenance(
            source=EnumOverlayValueSource.OPERATOR,
            actor=audit.actor,
            revision=new_revision,
            write_id=write_id,
        )
        record = write.model_copy(
            update={"revision": new_revision, "operator_paths": tuple(paths)}
        )
        reply = ModelOverlayWriteReply(
            write_id=write_id,
            overlay_id=overlay_id,
            outcome=EnumOverlayWriteOutcome.COMMITTED,
            revision=new_revision,
        )
        self._records[overlay_id] = self._held(record)
        self._provenance[overlay_id] = provenance
        self._audits = audits
        self._writes[write_id] = reply
        return reply

    def _write_audit(
        self,
        audits: dict[str, ModelOverlaySettingAudit],
        write_id: str,
        audit: ModelOverlaySettingAudit,
    ) -> None:
        """The audit table's insert: one row per write id."""
        if write_id in audits:
            raise ValueError(f"an audit row already holds write id {write_id}")
        audits[write_id] = audit


def _operator_paths(record: Mapping[str, JsonType] | None) -> tuple[str, ...]:
    held = None if record is None else record.get("operator_paths")
    return tuple(str(p) for p in held) if isinstance(held, list) else ()


def _loaded_at(record: Mapping[str, JsonType]) -> datetime:
    return datetime.fromisoformat(str(record["loaded_at"]))


__all__ = ["InMemoryOverlayStore"]
