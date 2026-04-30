# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch-record persistence for ``node_dispatch_worker``."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_record import (
    ModelDispatchRecord,
)


def _resolve_state_dir() -> Path:
    raw_state_dir = os.environ.get("ONEX_STATE_DIR")
    if not raw_state_dir:
        raise RuntimeError(
            "ONEX_STATE_DIR is not set; dispatch record persistence requires an "
            "explicit state directory."
        )
    return Path(raw_state_dir)


def write_dispatch_record(
    record: ModelDispatchRecord, *, state_dir: Path | str | None = None
) -> Path:
    """Persist *record* under ``$ONEX_STATE_DIR/dispatches/`` and return its path."""
    dispatches_dir = (
        Path(state_dir) if state_dir is not None else _resolve_state_dir()
    ) / "dispatches"
    dispatches_dir.mkdir(parents=True, exist_ok=True)
    out_path = dispatches_dir / f"{record.agent_id}.yaml"
    payload = record.model_dump(mode="json")
    out_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return out_path


__all__: list[str] = ["write_dispatch_record"]
