# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real host boundary, the overlay and the committed table of the lane toolchain provisioning effect."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.lane_toolchain import ModelLaneToolchainTable

from ..models.model_lane_toolchain_provision import ModelLaneToolchainProvisionOverlay

NODE_NAME = "node_lane_toolchain_provision_effect"
TABLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "node_host_capability_compute"
    / "lane_toolchain.yaml"
)
_TAIL_BYTES = 400
_RUN_TIMEOUT_S = 600


class HostFiles:
    """Filesystem and process operations on the machine the effect runs on."""

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

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

    def list_dir(self, path: str) -> tuple[str, ...] | None:
        try:
            return tuple(sorted(os.listdir(path)))
        except OSError:
            return None

    def remove_empty_dir(self, path: str) -> None:
        os.rmdir(path)

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        merged = {**os.environ, **env}
        try:
            done = subprocess.run(
                list(argv),
                env=merged,
                capture_output=True,
                text=True,
                check=False,
                timeout=_RUN_TIMEOUT_S,
            )
        except FileNotFoundError:
            return 127, "FileNotFoundError"
        except subprocess.TimeoutExpired:
            return 124, "TimeoutExpired"
        return done.returncode, (done.stdout + done.stderr)[-_TAIL_BYTES:].strip()


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLaneToolchainProvisionOverlay:
    """``<root>/node_lane_toolchain_provision_effect/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``; refuses with how to supply it."""
    return load_structured_overlay(
        NODE_NAME, ModelLaneToolchainProvisionOverlay, environ
    )


def load_table(text: str | None = None) -> ModelLaneToolchainTable:
    """The committed lane toolchain table. ``text`` replaces the packaged file (tests)."""
    return ModelLaneToolchainTable.from_yaml_text(
        text if text is not None else TABLE_PATH.read_text(encoding="utf-8")
    )
