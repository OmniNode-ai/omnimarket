# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The build store kept in one JSON file: the builds the node ran its checks on, newest last."""

from __future__ import annotations

import json
import os
from pathlib import Path

from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumCheckReading,
    ModelBuildRecord,
    ModelCheckRecord,
)


class FileBuildStore:
    def __init__(self, path: Path, keep: int) -> None:
        self._path = path
        self._keep = keep

    def _load(self) -> list[ModelBuildRecord]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return [ModelBuildRecord.model_validate(item) for item in raw]

    def _save(self, builds: list[ModelBuildRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                [b.model_dump(mode="json") for b in builds[-self._keep :]], indent=1
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    def begin(self, record: ModelBuildRecord) -> bool:
        builds = self._load()
        if any(b.build_id == record.build_id for b in builds):
            return False
        self._save([*builds, record])
        return True

    def previous_build(
        self, runtime_lane: str, build_id: str
    ) -> ModelBuildRecord | None:
        for build in reversed(self._load()):
            if build.runtime_lane == runtime_lane and build.build_id != build_id:
                return build
        return None

    def last_reading(
        self, check_id: str, before_build_id: str
    ) -> ModelCheckRecord | None:
        for build in reversed(self._load()):
            if build.build_id == before_build_id:
                continue
            for record in build.checks:
                if (
                    record.check_id == check_id
                    and record.reading is not EnumCheckReading.UNAVAILABLE
                ):
                    return record
        return None

    def finish(self, record: ModelBuildRecord) -> None:
        self._save(
            [record if b.build_id == record.build_id else b for b in self._load()]
        )


__all__ = ["FileBuildStore"]
