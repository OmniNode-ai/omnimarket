# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File adapters: the declared hold file (what lab placement reads on the host) and the node's own episode file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..models import ModelHoldRecord

HOLD_KEYS = ("host", "since", "hold_id", "hottest_c", "hold_above_c", "release_below_c")


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    staged.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staged, path)


class FileHoldStore:
    """The hold file in the guard's state directory. A file that exists and cannot be used is unreadable: the host stays held."""

    def __init__(self, state_dir: Path, hold_file: str) -> None:
        self._path = state_dir / hold_file

    def read(self) -> tuple[bool, ModelHoldRecord | None, str]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True, None, ""
        except OSError as exc:
            return False, None, f"{self._path.name}: {type(exc).__name__}"
        try:
            data = json.loads(text)
        except ValueError:
            return False, None, f"{self._path.name}: not JSON"
        if not isinstance(data, dict) or any(key not in data for key in HOLD_KEYS):
            return False, None, f"{self._path.name}: not a hold record"
        try:
            record = ModelHoldRecord(
                str(data["host"]),
                str(data["since"]),
                str(data["hold_id"]),
                float(data["hottest_c"]),
                float(data["hold_above_c"]),
                float(data["release_below_c"]),
            )
        except (TypeError, ValueError):
            return False, None, f"{self._path.name}: a value has the wrong type"
        return True, record, ""

    def write(self, record: ModelHoldRecord) -> None:
        _write_atomic(self._path, record.as_dict())

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class FileEpisodeStore:
    """The node's own episode file under its state directory."""

    def __init__(self, state_dir: Path, episode_file: str) -> None:
        self._path = state_dir / episode_file

    def load(self) -> dict[str, Any] | None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, episode: dict[str, Any]) -> None:
        _write_atomic(self._path, episode)
