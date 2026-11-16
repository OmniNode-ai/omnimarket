# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the unit tree install tests: a throwaway git repository, an overlay row and a root."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
)
from omnimarket.nodes.node_unit_tree_install_effect.overlay import (
    ModelUnitTreeInstallOverlay,
    ModelUnitTreeRow,
)

ROOT_ENV = "UNIT_TREE_TEST_ROOT"
PASS = (sys.executable, "-c", "pass")
FAIL = (sys.executable, "-c", "raise SystemExit(1)")


def git(clone: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def commit_all(clone: Path, message: str) -> str:
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", message)
    return git(clone, "rev-parse", "HEAD")


def make_root(tmp_path: Path) -> Path:
    """A root holding clone/ with one commit on main (a.txt, b.txt)."""
    root = tmp_path / "root"
    clone = root / "clone"
    clone.mkdir(parents=True)
    git(clone, "init", "-q", "-b", "main")
    (clone / "a.txt").write_text("a1\n", encoding="utf-8")
    (clone / "b.txt").write_text("b1\n", encoding="utf-8")
    commit_all(clone, "one")
    return root


def make_overlay(
    smoke: tuple[tuple[str, ...], ...] = (PASS,),
    sync: tuple[str, ...] = PASS,
    host: str = "h1",
) -> ModelUnitTreeInstallOverlay:
    row = ModelUnitTreeRow(
        name="u1",
        host=host,
        clone_relative="clone",
        ref="main",
        install_relative="units/u1/tree",
        record_relative="units/u1/record.json",
        preserve_paths=(".venv",),
        sync_command=sync,
        smoke_commands=smoke,
        command_timeout_seconds=60,
        keep=2,
    )
    return ModelUnitTreeInstallOverlay(omni_root_env=ROOT_ENV, units=(row,))


def request(
    operation: Literal["install", "verify", "dry-run"] = "install",
    unit: str = "u1",
    host: str = "h1",
    ref: str = "",
) -> ModelUnitTreeInstallRequest:
    return ModelUnitTreeInstallRequest(
        operation=operation, unit=unit, host=host, ref=ref
    )


def environ(root: Path) -> dict[str, str]:
    return {ROOT_ENV: str(root)}
