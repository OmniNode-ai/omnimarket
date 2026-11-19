# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File adapters: the guard's state directory (read, plus the outbox claim) and the node's own episode file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..models import EnumGuardReadState, ModelGuardRead

#: The keys of the guard's latch the alarm needs; a latch without them is a guard of another shape.
LATCH_KEYS = ("tripped_at", "hottest_c")
LAST_KEYS = ("hottest_c", "level")


def _read_object(path: Path, required: tuple[str, ...]) -> ModelGuardRead:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ModelGuardRead(EnumGuardReadState.ABSENT)
    except OSError as exc:
        return ModelGuardRead(
            EnumGuardReadState.UNREADABLE, None, f"{path.name}: {type(exc).__name__}"
        )
    try:
        data = json.loads(text)
    except ValueError:
        return ModelGuardRead(
            EnumGuardReadState.UNREADABLE, None, f"{path.name}: not JSON"
        )
    if not isinstance(data, dict):
        return ModelGuardRead(
            EnumGuardReadState.UNREADABLE, None, f"{path.name}: not an object"
        )
    missing = [key for key in required if key not in data]
    if missing:
        return ModelGuardRead(
            EnumGuardReadState.SHAPE_CHANGED,
            None,
            f"{path.name}: no {', '.join(missing)}",
        )
    return ModelGuardRead(EnumGuardReadState.PRESENT, data)


class GuardFileState:
    """The guard's state directory. The outbox is claimed by renaming it to ``<outbox>.<claim_suffix>``: the guard
    appends to the outbox name with open(..., "a"), so a row it writes after the rename lands in a new file."""

    def __init__(
        self,
        state_dir: Path,
        latch_file: str,
        last_file: str,
        outbox_file: str,
        refused_file: str,
        claim_suffix: str,
    ) -> None:
        self._latch = state_dir / latch_file
        self._last = state_dir / last_file
        self._outbox = state_dir / outbox_file
        self._refused = state_dir / refused_file
        self._claim = state_dir / f"{outbox_file}{claim_suffix}"

    def read_latch(self) -> ModelGuardRead:
        return _read_object(self._latch, LATCH_KEYS)

    def read_last(self) -> ModelGuardRead:
        return _read_object(self._last, LAST_KEYS)

    @staticmethod
    def _lines(path: Path) -> list[str] | None:
        try:
            return [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError):
            return None

    def peek_outbox(self) -> list[str] | None:
        held = self._lines(self._claim)
        queued = self._lines(self._outbox)
        return None if held is None or queued is None else [*held, *queued]

    def claim_outbox(self) -> list[str] | None:
        if not self._claim.exists():
            try:
                os.replace(self._outbox, self._claim)
            except FileNotFoundError:
                return []
            except OSError:
                return None
        return self._lines(self._claim)

    def settle_outbox(self, kept: list[str], refused: list[tuple[str, str]]) -> None:
        if refused:
            with self._refused.open("a", encoding="utf-8") as handle:
                for row, reason in refused:
                    handle.write(f"{row} | refused: {' '.join(reason.split())}\n")
        if kept:
            with self._outbox.open("a", encoding="utf-8") as handle:
                for row in kept:
                    handle.write(f"{row}\n")
        self._claim.unlink(missing_ok=True)


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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        staged = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        staged.write_text(json.dumps(episode, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staged, self._path)
