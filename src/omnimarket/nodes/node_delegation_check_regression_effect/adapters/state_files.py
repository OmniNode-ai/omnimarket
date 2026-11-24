# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's two state files under the registry root: the transitions already raised and the dev builds seen."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    EnumChangeKind,
    ModelChange,
    ModelRebuildCompletedObserved,
)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + f".{os.getpid()}.tmp")
    scratch.write_text(text, encoding="utf-8")
    scratch.replace(path)


class FileSeenStore:
    """The ids of the transitions already raised, kept in one JSON file; an unreadable file reads as empty-and-raises."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._ids: set[str] | None = None

    def _load(self) -> set[str]:
        if self._ids is None:
            try:
                document = json.loads(self._path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._ids = set()
            else:
                if not isinstance(document, dict) or not isinstance(
                    document.get("seen"), list
                ):
                    raise OSError(f"{self._path}: not a seen record")
                self._ids = {str(x) for x in document["seen"]}
        return self._ids

    def seen(self, event_id: str) -> bool:
        return event_id in self._load()

    def record(self, event_id: str) -> None:
        ids = self._load()
        ids.add(event_id)
        _write_atomic(self._path, json.dumps({"seen": sorted(ids)}, indent=1))


class FileDevBuildStore:
    """One JSON line per rebuild the deploy agent completed on the watched lane."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, build: ModelRebuildCompletedObserved) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(build.model_dump_json() + "\n")

    def between(self, start: datetime, end: datetime) -> tuple[ModelChange, ...]:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ()
        changes: list[ModelChange] = []
        for line in lines:
            try:
                build = ModelRebuildCompletedObserved.model_validate_json(line)
            except ValueError:
                continue
            if start < build.completed_at <= end:
                changes.append(
                    ModelChange(
                        kind=EnumChangeKind.DEV_BUILD,
                        at=build.completed_at,
                        ref=build.git_sha,
                        summary=f"{build.runtime_lane} rebuild at {build.requested_git_ref or build.git_sha[:12]}",
                    )
                )
        return tuple(changes)
