# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory and file-backed bindings for fact reads and atomic audit appends."""

import fcntl
import json
from collections.abc import Mapping
from pathlib import Path

from omnimarket.models.standing_rules.model_standing_rules import (
    ModelStandingRulesAudit,
)


class MemoryStandingRulesIO:
    def __init__(self, documents: Mapping[str, str]) -> None:
        self.documents = dict(documents)
        self.rows: dict[str, dict[str, ModelStandingRulesAudit]] = {}

    def read(self, path: str) -> str:
        if path not in self.documents:
            raise FileNotFoundError(path)
        return self.documents[path]

    def append_once(self, path: str, key: str, row: ModelStandingRulesAudit) -> None:
        self.rows.setdefault(path, {}).setdefault(key, row)


class FileStandingRulesIO:
    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def append_once(self, path: str, key: str, row: ModelStandingRulesAudit) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            keys = {json.loads(line)["key"] for line in stream if line.strip()}
            if key not in keys:
                stream.write(
                    json.dumps({"key": key, **row.model_dump(mode="json")}) + "\n"
                )
                stream.flush()


def memory_standing_rules_io(config: Mapping[str, str]) -> object:
    return MemoryStandingRulesIO(config)


def file_standing_rules_io(config: Mapping[str, str]) -> object:
    return FileStandingRulesIO()
