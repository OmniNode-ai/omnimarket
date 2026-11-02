# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real host boundary, the overlay and the committed table of the lane reader provisioning effect."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from ..models.model_lane_reader_provision import (
    ModelLaneReaderProvisionOverlay,
    ModelLaneReaderTableRow,
)

NODE_NAME = "node_lane_reader_provision_effect"
TABLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "node_host_capability_compute"
    / "lane_reader_inputs.yaml"
)
_TAIL_BYTES = 400


class HostFiles:
    """Filesystem and process operations on the machine the effect runs on."""

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def read_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write_text_atomic(self, path: str, text: str) -> None:
        target = Path(path)
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def link_target(self, path: str) -> str | None:
        try:
            return os.readlink(path)
        except OSError:
            return None

    def replace_link(self, path: str, target: str) -> None:
        link = Path(path)
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp = link.with_name(f".{link.name}.{os.getpid()}")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target)
        os.replace(tmp, link)

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        merged = {**os.environ, **env}
        try:
            done = subprocess.run(
                list(argv),
                env=merged,
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return 127, type(exc).__name__
        return done.returncode, (done.stdout + done.stderr)[-_TAIL_BYTES:].strip()


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLaneReaderProvisionOverlay:
    """``<root>/node_lane_reader_provision_effect/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``; refuses with how to supply it."""
    return load_structured_overlay(NODE_NAME, ModelLaneReaderProvisionOverlay, environ)


def load_table(text: str | None = None) -> dict[str, ModelLaneReaderTableRow]:
    """The committed declared-host table, by host. ``text`` replaces the packaged file (tests)."""
    raw = (
        yaml.safe_load(
            text if text is not None else TABLE_PATH.read_text(encoding="utf-8")
        )
        or {}
    )
    return {
        name: ModelLaneReaderTableRow(host=name, **(fields or {}))
        for name, fields in (raw.get("hosts") or {}).items()
    }
