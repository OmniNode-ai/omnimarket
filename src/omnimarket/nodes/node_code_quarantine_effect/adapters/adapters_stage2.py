# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stage 2 seams: the loaded-copy reader (in memory and from a dev reading file) and an in-memory reporter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_code_quarantine_effect.models import ModelStage2Event
from omnimarket.nodes.node_code_quarantine_effect.protocols import (
    LoadedCopyUnreadableError,
)


class InMemoryLoadedCopies:
    """Each contract name maps to the repository dev loads, or to None when the reading shows none is loaded.

    A name that is absent was not read: it raises, as an unreachable dev does.
    """

    def __init__(self, loaded: Mapping[str, str | None]) -> None:
        self.loaded = dict(loaded)

    def loaded_repository(self, contract_name: str) -> str:
        if contract_name not in self.loaded:
            raise LoadedCopyUnreadableError(f"no reading of {contract_name}")
        return self.loaded[contract_name] or ""


class JsonFileLoadedCopies:
    """Reads a dev reading file: ``{"loaded_contracts": {"<contract name>": "<repository>" | null}}``.

    A missing or malformed file, and a name the file does not list, are unread. Absence from the reading is never
    taken for "dev loads none".
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def loaded_repository(self, contract_name: str) -> str:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            listed = data["loaded_contracts"]
            if not isinstance(listed, dict):
                raise TypeError("loaded_contracts is not an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LoadedCopyUnreadableError(f"{self._path}: {exc}") from exc
        if contract_name not in listed:
            raise LoadedCopyUnreadableError(
                f"{self._path} does not list {contract_name}"
            )
        value = listed[contract_name]
        if value is not None and not isinstance(value, str):
            raise LoadedCopyUnreadableError(
                f"{self._path} lists {contract_name} as {value!r}"
            )
        return value or ""


class InMemoryStage2Reporter:
    def __init__(self) -> None:
        self.events: list[ModelStage2Event] = []

    def report(self, event: ModelStage2Event) -> None:
        self.events.append(event)
