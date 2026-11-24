# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``schedule`` reads the floor fill overlay from the settings store's root before the files, so a set key reaches the unit."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_schedule import (
    load_schedule_overlay,
)

NODE = "node_floor_fill_orchestrator"
STORE_RELPATH = ".local/state/onex/overlay-store"


def _write_overlay(root: Path, host_id: str) -> None:
    folder = root / NODE
    folder.mkdir(parents=True)
    (folder / "overlay.yaml").write_text(
        yaml.safe_dump({"floor_fill": {"host_id": host_id}}), encoding="utf-8"
    )


def _environ(tmp_path: Path, declared: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "OMNIBASE_PATH": str(tmp_path / "registry" / "root"),
        "ONEX_SKILL_OVERLAY_ROOTS": str(declared),
    }


def test_store_overlay_is_read_before_the_declared_file(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    _write_overlay(declared, "from-file")
    _write_overlay(tmp_path / "home" / STORE_RELPATH, "from-store")
    overlay = load_schedule_overlay(_environ(tmp_path, declared))
    assert overlay == {"floor_fill": {"host_id": "from-store"}}


def test_declared_file_is_read_when_the_store_holds_no_floor_fill_document(
    tmp_path: Path,
) -> None:
    declared = tmp_path / "declared"
    _write_overlay(declared, "from-file")
    (tmp_path / "home" / STORE_RELPATH).mkdir(parents=True)
    overlay = load_schedule_overlay(_environ(tmp_path, declared))
    assert overlay == {"floor_fill": {"host_id": "from-file"}}


def test_no_overlay_anywhere_is_none(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    declared.mkdir()
    assert load_schedule_overlay(_environ(tmp_path, declared)) is None
