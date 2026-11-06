# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outbox health on one host: queued ledger rows too old to pass, or refused on replay.

A writer whose bus publish fails queues the row beside the host's ledger and returns success, so
nothing else notices. A queued row more than a day old can no longer pass the serve's clock guard,
so an outbox nobody reads loses rows. A refused file means a writer was told its row was queued and
it never landed. Named break: plant a queued request older than the bound, or a refused file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.model_host_reading import EnumHostProbe, ModelProbeReading
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.overlay import ModelOutboxProbeSettings
from omnimarket.nodes.node_host_reading_effect.protocols import ProtocolHostFiles

_PROBE = EnumHostProbe.LEDGER_OUTBOX


def _unknown(reason: str) -> ModelProbeReading:
    return ModelProbeReading(
        probe=_PROBE,
        verdict=EnumReadingVerdict.UNKNOWN,
        outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
        reasons=(reason,),
    )


def _queued_at(text: str, field: str) -> datetime | None:
    try:
        body = json.loads(text)
        stamp = body[field]
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, KeyError, TypeError):
        return None


def read_outbox(
    settings: ModelOutboxProbeSettings,
    files: ProtocolHostFiles,
    environ: Mapping[str, str],
    now: datetime,
) -> ModelProbeReading:
    configured = environ.get(settings.ledger_path_env, "").strip()
    if not configured:
        return _unknown(f"configured-path-unset-{settings.ledger_path_env.lower()}")
    ledger = files.resolve(Path(configured))
    locks = ledger.parent / settings.lock_dirname
    outboxes = files.list_names(locks, f"{ledger.name}.*{settings.outbox_suffix}")
    pending = refused = unreadable = 0
    oldest: datetime | None = None
    for name in outboxes:
        directory = locks / name
        for entry in files.list_names(directory, settings.pending_glob):
            pending += 1
            queued = _queued_at(
                files.read_text(directory / entry), settings.queued_at_field
            )
            if queued is None:
                unreadable += 1
            elif oldest is None or queued < oldest:
                oldest = queued
        refused += len(
            files.list_names(
                directory / settings.refused_dirname, settings.pending_glob
            )
        )
    age = None if oldest is None else max(0, int((now - oldest).total_seconds()))
    reasons: list[str] = []
    if age is not None and age > settings.oldest_max_seconds:
        reasons.append(f"oldest-over-{settings.oldest_max_seconds}s")
    if refused > settings.refused_max:
        reasons.append(f"refused-over-{settings.refused_max}")
    if unreadable:
        reasons.append("queued-file-unreadable")
    return ModelProbeReading(
        probe=_PROBE,
        verdict=EnumReadingVerdict.NOT_WORKING
        if reasons
        else EnumReadingVerdict.WORKING,
        outcome=EnumReadingOutcome.READ,
        reasons=tuple(reasons) or ("outbox-within-bounds",),
        cells={
            "pending": str(pending),
            "oldest_s": "-" if age is None else str(age),
            "refused": str(refused),
        },
    )
