# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integrated, hosted: the node runs the way an operator runs it, `python -m` in a subprocess under an overlay root."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from tests.nodes.node_unit_tree_install_effect.unit_tree_support import (
    ROOT_ENV,
    make_overlay,
    make_root,
    request,
)

MODULE = "omnimarket.nodes.node_unit_tree_install_effect"


def _run(
    tmp_path: Path, root: Path, body: str, with_overlay: bool = True
) -> subprocess.CompletedProcess[str]:
    overlays = tmp_path / "overlays"
    if with_overlay:
        node_dir = overlays / "node_unit_tree_install_effect"
        node_dir.mkdir(parents=True)
        (node_dir / "overlay.yaml").write_text(
            yaml.safe_dump(make_overlay().model_dump(mode="json")), encoding="utf-8"
        )
    env = {**os.environ, ROOT_ENV: str(root), "ONEX_SKILL_OVERLAY_ROOTS": str(overlays)}
    return subprocess.run(
        [sys.executable, "-m", MODULE],
        input=body,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )


def test_runtime_entry_installs_the_tree_from_the_overlay_row(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    done = _run(tmp_path, root, request().model_dump_json())
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["verdict"] == "pass"
    assert (root / "units" / "u1" / "tree" / "a.txt").exists()
    assert (root / "units" / "u1" / "record.json").is_file()


def test_runtime_entry_exits_1_for_a_unit_the_overlay_does_not_name(
    tmp_path: Path,
) -> None:
    root = make_root(tmp_path)
    done = _run(tmp_path, root, request(unit="other").model_dump_json())
    assert done.returncode == 1
    assert json.loads(done.stdout)["verdict"] == "refused"
    assert not (root / "units").exists()


def test_runtime_entry_without_an_overlay_installs_nothing(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    done = _run(tmp_path, root, request().model_dump_json(), with_overlay=False)
    assert done.returncode != 0
    assert not (root / "units").exists()
