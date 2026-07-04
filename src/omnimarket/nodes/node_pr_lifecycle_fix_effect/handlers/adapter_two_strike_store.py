# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persistent two-strike counter for the delegation eligibility gate.

Safety bar #7 (WS-D/D2, OMN-13940): a second delegation failure on the same
PR/block_reason must permanently escalate that PR/block_reason to the agent
(Claude) path. Merge-sweep runs as a fresh process per tick, so the counter
must survive across process invocations — an in-memory counter would silently
reset every tick and never trip. Persistence is a plain JSON file under
``ONEX_STATE_DIR`` (falling back to ``$OMNI_HOME/.onex_state``), matching the
breadcrumb pattern already used by ``PrPolishDispatchAdapter``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class ProtocolTwoStrikeStore(Protocol):
    """Seam for the two-strike counter so tests can inject an in-memory fake."""

    def get_strikes(self, key: str) -> int:
        """Return the current failure count for ``key`` (0 if never recorded)."""
        ...

    def record_failure(self, key: str) -> int:
        """Increment and persist the failure count for ``key``. Returns new count."""
        ...


def strike_key(repo: str, pr_number: int, block_reason: str) -> str:
    """Canonical two-strike key: one counter per PR per block_reason."""
    return f"{repo}#{pr_number}:{block_reason}"


class JsonFileTwoStrikeStore:
    """File-backed two-strike counter, one JSON object per state dir."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._path = (
            (state_dir or self._resolve_state_dir())
            / "delegated_fix"
            / ("two_strike.json")
        )

    @staticmethod
    def _resolve_state_dir() -> Path:
        raw = os.environ.get("ONEX_STATE_DIR")
        if raw:
            return Path(raw)
        omni_home = os.environ.get("OMNI_HOME")
        if omni_home:
            return Path(omni_home) / ".onex_state"
        raise RuntimeError(
            "ONEX_STATE_DIR or OMNI_HOME must be set for two-strike persistence; "
            "the delegation eligibility gate cannot durably record failures "
            "without one of them."
        )

    def _read(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "two_strike_store: failed to read %s, treating as empty: %s",
                self._path,
                exc,
            )
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): int(v) for k, v in raw.items()}

    def _write(self, data: dict[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def get_strikes(self, key: str) -> int:
        return self._read().get(key, 0)

    def record_failure(self, key: str) -> int:
        data = self._read()
        new_count = data.get(key, 0) + 1
        data[key] = new_count
        self._write(data)
        return new_count


__all__: list[str] = [
    "JsonFileTwoStrikeStore",
    "ProtocolTwoStrikeStore",
    "strike_key",
]
