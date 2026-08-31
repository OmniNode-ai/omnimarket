# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable state helpers for node_wave_scheduler_orchestrator [OMN-17017].

The wave scheduler's substrate is **durable dispatch-lifecycle records under
``$ONEX_STATE_DIR``** — append-only NDJSON plus a per-run checkpoint. Nothing is
inferred from selection: a ticket is complete only when a terminal outcome
record says so.

This module holds the path algebra and the checkpoint read/write, so the run
identity a handler computes and the one a dispatcher writes under can never
drift apart.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelWaveCheckpoint(BaseModel):
    """Observed progress for one wave-scheduler run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Run identity the checkpoint belongs to.")
    plan_path: str = Field(description="Plan file the run was computed from.")
    completed_ticket_ids: tuple[str, ...] = Field(
        default=(),
        description="Tickets observed COMPLETED. Only these are skipped on resume.",
    )


def resolve_state_dir(state_dir: str | None) -> Path:
    """Resolve the durable state root, failing fast when nothing is configured."""
    if state_dir:
        return Path(state_dir)
    configured = os.environ.get("ONEX_STATE_DIR")  # contract-config-ok: config
    if configured:
        return Path(configured)
    return Path(os.environ["OMNI_HOME"]) / ".onex_state"


def run_id_for(plan_path: str) -> str:
    """Deterministic run identity for a plan path.

    Stable across invocations so ``--resume`` finds the checkpoint the previous
    run wrote, and readable enough to grep for in the lifecycle log.
    """
    digest = hashlib.sha256(plan_path.encode("utf-8")).hexdigest()[:12]
    return f"{Path(plan_path).stem}-{digest}"


def run_dir(state_dir: Path, run_id: str) -> Path:
    return state_dir / "wave_scheduler" / run_id


def checkpoint_path(state_dir: Path, run_id: str) -> Path:
    return run_dir(state_dir, run_id) / "checkpoint.json"


def load_checkpoint(state_dir: Path, run_id: str) -> ModelWaveCheckpoint | None:
    """Return the persisted checkpoint, or None when the run has none yet."""
    path = checkpoint_path(state_dir, run_id)
    if not path.is_file():
        return None
    return ModelWaveCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))


def write_checkpoint(state_dir: Path, checkpoint: ModelWaveCheckpoint) -> Path:
    """Persist observed progress after a wave settles."""
    path = checkpoint_path(state_dir, checkpoint.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkpoint.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "ModelWaveCheckpoint",
    "checkpoint_path",
    "load_checkpoint",
    "resolve_state_dir",
    "run_dir",
    "run_id_for",
    "write_checkpoint",
]
