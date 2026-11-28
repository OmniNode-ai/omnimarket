# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The first form of the bootstrap seed: each slice overlay's tested default through the write path, and a host's bootstrap file.

A run refuses, naming the input, before it writes anything when a store, broker or Infisical input is missing from the
intake. An overlay whose newest stored digest is already the default's writes nothing, so a second run writes nothing. After
the writes, the table's newest record of each overlay must carry the digest that was written and a revision. The table
(the database the projection writes, read through its snapshot) is the settings store of record; nothing else is compared.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime

from omnibase_spi.protocols.storage import ProtocolOverlayStore
from pydantic import BaseModel, ConfigDict, JsonValue

from omnimarket.handlers.overlay_store.bootstrap_file import render_bootstrap_file
from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest

WritePath = Callable[[Mapping[str, JsonValue]], Awaitable[None]]


class SeedDisagreementError(RuntimeError):
    """The table and the settings store hold different records of an overlay after the seed."""


class ModelSeedResult(BaseModel):
    """What one seed run did, per overlay id, and the bootstrap file it rendered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    written: tuple[str, ...]
    unchanged: tuple[str, ...]
    bootstrap_file: str


async def seed_defaults(
    intake: Mapping[str, str],
    defaults: Mapping[str, Mapping[str, JsonValue]],
    *,
    table: ProtocolOverlayStore,
    write: WritePath,
    source_ref: str,
    loaded_by: str,
    now: datetime,
) -> ModelSeedResult:
    """Seed each default the table lacks through ``write``, render the host's bootstrap file, then check the table holds what was written."""
    bootstrap_file = render_bootstrap_file(intake)  # refuses by name before any write
    written: list[str] = []
    unchanged: list[str] = []
    for overlay_id, document in sorted(defaults.items()):
        digest = document_digest(dict(document))
        current = await table.read_current(overlay_id)
        if current is not None and current.get("digest") == digest:
            unchanged.append(overlay_id)
            continue
        record = ModelOverlayStoreRecord(
            overlay_id=overlay_id,
            digest=digest,
            document=dict(document),
            source_ref=source_ref,
            loaded_by=loaded_by,
            loaded_at=now,
        )
        await write(record.model_dump(mode="json"))
        written.append(overlay_id)
    for overlay_id in sorted(defaults):
        await _require_agreement(
            overlay_id, table, document_digest(dict(defaults[overlay_id]))
        )
    return ModelSeedResult(
        written=tuple(written),
        unchanged=tuple(unchanged),
        bootstrap_file=bootstrap_file,
    )


async def _require_agreement(
    overlay_id: str, table: ProtocolOverlayStore, digest: str
) -> None:
    held = await table.read_current(overlay_id)
    if held is None:
        raise SeedDisagreementError(
            f"overlay {overlay_id}: the table holds no record after the seed"
        )
    if held.get("digest") != digest:
        raise SeedDisagreementError(
            f"overlay {overlay_id}: the table's digest is not the default's after the seed"
        )


__all__ = ["ModelSeedResult", "SeedDisagreementError", "seed_defaults"]
