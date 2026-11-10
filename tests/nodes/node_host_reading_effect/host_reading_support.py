# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: one host world on either backend (in memory or the real filesystem)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_reading_effect.adapters.host_files import (
    InMemoryHostFiles,
    LocalHostFiles,
)
from omnimarket.nodes.node_host_reading_effect.overlay import ModelHostReadingOverlay
from omnimarket.nodes.node_host_reading_effect.protocols import ProtocolHostFiles

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_reading_effect/overlay.example.yaml"
)
RUN_AT = datetime(2026, 11, 5, 18, 25, tzinfo=UTC)
LEDGER_HOST = "example-ledger-host"
WRITING_HOST = "example-writing-host"
OLD_LEDGER = "docs/old/LEDGER.md"
OLD_ARCHIVE = "docs/old/archive"


def overlay() -> ModelHostReadingOverlay:
    return ModelHostReadingOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def queued(age_seconds: int, request: str = "r1") -> str:
    stamp = RUN_AT - timedelta(seconds=age_seconds)
    return json.dumps(
        {
            "request_id": request,
            "payload": "row\n",
            "queued_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )


class World:
    """A ledger host's files with the old names linked; a test breaks one thing by name."""

    def __init__(self, backend: str, base: Path) -> None:
        self.base = base
        self.root = base / "home"
        self.ledger = base / "state/LEDGER.md"
        self.environ = {
            "OMNI_HOME": str(self.root),
            "ONEX_LEDGER_PATH": str(self.ledger),
        }
        self.memory = InMemoryHostFiles() if backend == "memory" else None
        self.files: ProtocolHostFiles = self.memory or LocalHostFiles()
        self.write(self.ledger, "# ledger\n")
        self.write(base / "state/archive/roll.md", "")
        self.link(self.root / OLD_LEDGER, self.ledger)
        self.link(self.root / OLD_ARCHIVE, base / "state/archive")

    @property
    def outbox(self) -> Path:
        return self.ledger.parent / ".ledger_locks" / "LEDGER.md.abc.bus-outbox"

    def write(self, path: Path, text: str) -> None:
        if self.memory is not None:
            self.memory.add_file(path, text)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def link(self, path: Path, target: Path) -> None:
        if self.memory is not None:
            self.memory.links[path] = target
            self.memory.directories.add(path.parent)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)

    # -- the named breaks ------------------------------------------------------------------

    def break_restore_old_name_as_regular_file(self) -> None:
        old = self.root / OLD_LEDGER
        if self.memory is not None:
            del self.memory.links[old]
            self.memory.add_file(old, "# restored\n")
            return
        old.unlink()
        old.write_text("# restored\n", encoding="utf-8")

    def break_remove_old_name(self) -> None:
        old = self.root / OLD_ARCHIVE
        if self.memory is not None:
            del self.memory.links[old]
            return
        old.unlink()

    def break_point_old_name_elsewhere(self) -> None:
        old = self.root / OLD_LEDGER
        other = self.base / "state/other.md"
        self.write(other, "")
        if self.memory is not None:
            self.memory.links[old] = other
            return
        old.unlink()
        old.symlink_to(other)

    def break_stage_regular_file_at_old_name(self) -> None:
        """The name stays a link on disk; only the index holds a file there."""
        if self.memory is not None:
            self.memory.index[OLD_LEDGER] = "100644"
            return
        old = self.root / OLD_LEDGER
        subprocess.run(
            ["git", "init", "-q", str(self.root)],
            check=True,
            env=scrub_git_location_env(os.environ),
        )
        old.unlink()
        old.write_text("# staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", OLD_LEDGER],
            check=True,
            env=scrub_git_location_env(os.environ),
        )
        old.unlink()
        old.symlink_to(self.ledger)

    def break_plant_stale_queued_request(self, age_seconds: int = 901) -> None:
        self.write(self.outbox / "0001_r1.json", queued(age_seconds))

    def break_plant_refused_file(self) -> None:
        self.write(self.outbox / "refused" / "0002_r2.json", queued(10, "r2"))
