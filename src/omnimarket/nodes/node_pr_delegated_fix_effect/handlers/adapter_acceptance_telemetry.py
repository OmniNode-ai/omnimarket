# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance telemetry for delegated-fix attempts (Slice 1, OMN-16868).

OMN-13940 gates any widening of the delegation ladder on **>=70% acceptance
over >=20 samples**. Slice 0 recorded nothing per attempt, so that bar was
stated but not measurable. This module is the sink that makes it checkable.

One JSONL row per *terminal* delegated-fix attempt — accepted, refused, gate-
failed, or errored alike. Refusals and gate failures MUST land in the file:
if only successes were recorded the acceptance rate would be 100% by
construction and the bar unfalsifiable.

Persistence deliberately reuses the Slice 0 breadcrumb pattern established by
``JsonFileTwoStrikeStore`` — a plain file under ``ONEX_STATE_DIR`` (falling
back to ``$OMNI_HOME/.onex_state``), in the same ``delegated_fix/``
subdirectory — because merge-sweep runs as a fresh process per tick and an
in-memory counter would reset before 20 samples ever accumulated.

Telemetry is observability, never a gate: every write failure is swallowed and
logged. A sink outage must not fail a delegation that otherwise succeeded.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

logger = logging.getLogger(__name__)

_TELEMETRY_FILENAME = "acceptance_telemetry.jsonl"


class EnumPlacementReason(StrEnum):
    """Why THIS backend carried this attempt (OMN-16891).

    The record already answers *what ran* and *how it went*; without the cause
    of the placement the acceptance rate cannot be attributed. A 60% rate on a
    free cloud rung means something different when that rung was chosen for its
    capability than when it was chosen because the local slot was busy — the
    first is a quality signal about the model, the second is a capacity signal
    about the fleet. Collapsing them makes the OMN-13940 widening bar
    (>=70% over >=20 samples) unattributable, which is how a placement table
    ends up re-argued instead of tuned.

    A closed enum rather than free text, so rows aggregate.
    """

    LOCAL_FIRST = "local-first"
    """Cheapest-first put this on owned GPUs; nothing forced a cloud rung."""

    SATURATION_ESCALATE = "saturation-escalate"
    """The local slot stayed busy past the contract's bounded wait."""

    CLASS_AFFINITY = "class-affinity"
    """The task class declares this tier/backend as its preferred placement."""

    FALLBACK = "fallback"
    """An earlier rung failed (transport, quota, or quality) and this caught it."""


class ModelDelegatedFixAttemptRecord(BaseModel):
    """One delegated-fix attempt, accepted or not.

    ``accepted`` is the numerator of the OMN-13940 bar; every row is a
    denominator sample. ``task_type`` is the delegation task class that ran
    (``"document"`` on the Slice 1 path) or ``None`` for the deterministic
    ruff path, so the bar can be computed per class rather than smeared across
    both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    repo: str = Field(...)
    pr_number: int = Field(...)
    block_reason: str = Field(...)
    task_type: str | None = Field(
        default=None,
        description="Delegation task class ('document'), or None for ruff.",
    )
    delegation_model: str = Field(...)
    backend_id: str | None = Field(default=None)
    tier: str | None = Field(default=None)
    # OMN-16891: WHY this backend, not just which one. Defaults to None so the
    # Slice 1 rows written before this field existed still validate on
    # readback — the model is frozen + extra="forbid", so a required field here
    # would make every historical sample unreadable and reset the >=20-sample
    # denominator to zero.
    placement_reason: EnumPlacementReason | None = Field(
        default=None,
        description="Why this backend carried the attempt (OMN-16891).",
    )
    outcome: str = Field(..., description="EnumDelegatedFixOutcome value.")
    accepted: bool = Field(..., description="Numerator of the >=70% bar.")
    cost_usd: float = Field(default=0.0, ge=0.0)
    files_changed: NonNegativeInt = Field(default=0)
    lines_changed: NonNegativeInt = Field(default=0)
    recorded_at: datetime = Field(...)


@runtime_checkable
class ProtocolAcceptanceRecorder(Protocol):
    """Seam for the acceptance sink so tests can inject an in-memory fake."""

    def record(self, record: ModelDelegatedFixAttemptRecord) -> None:
        """Persist one attempt. MUST NOT raise."""
        ...


def _resolve_state_dir() -> Path:
    raw = os.environ.get("ONEX_STATE_DIR")
    if raw:
        return Path(raw)
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        return Path(omni_home) / ".onex_state"
    raise RuntimeError(
        "ONEX_STATE_DIR or OMNI_HOME must be set for delegated-fix acceptance "
        "telemetry; the >=70%/>=20-sample widening bar cannot be measured "
        "without a durable sink."
    )


class JsonlAcceptanceTelemetryRecorder:
    """Append-only JSONL sink, one row per delegated-fix attempt."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._path = (
            (state_dir or _resolve_state_dir()) / "delegated_fix" / _TELEMETRY_FILENAME
        )

    @property
    def path(self) -> Path:
        return self._path

    def record(self, record: ModelDelegatedFixAttemptRecord) -> None:
        """Append one row. Never raises — telemetry is not a gate."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = record.model_dump_json()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, ValueError) as exc:
            logger.warning(
                "acceptance_telemetry: failed to record attempt for %s#%s: %s",
                record.repo,
                record.pr_number,
                exc,
            )

    def read_samples(
        self, *, task_type: str | None = None
    ) -> list[ModelDelegatedFixAttemptRecord]:
        """Read back every recorded attempt, optionally filtered by task class.

        This is what makes the OMN-13940 bar checkable:

            samples = recorder.read_samples(task_type="document")
            rate = sum(s.accepted for s in samples) / len(samples)
            widening_allowed = len(samples) >= 20 and rate >= 0.70
        """
        if not self._path.exists():
            return []
        samples: list[ModelDelegatedFixAttemptRecord] = []
        try:
            raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning(
                "acceptance_telemetry: unreadable sink %s: %s", self._path, exc
            )
            return []
        for raw in raw_lines:
            if not raw.strip():
                continue
            try:
                samples.append(ModelDelegatedFixAttemptRecord(**json.loads(raw)))
            except (ValueError, TypeError) as exc:
                logger.warning("acceptance_telemetry: skipping malformed row: %s", exc)
        if task_type is not None:
            samples = [s for s in samples if s.task_type == task_type]
        return samples


__all__: list[str] = [
    "EnumPlacementReason",
    "JsonlAcceptanceTelemetryRecorder",
    "ModelDelegatedFixAttemptRecord",
    "ProtocolAcceptanceRecorder",
]
