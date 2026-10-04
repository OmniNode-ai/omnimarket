# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The post-release dev bump leaves dev ahead of every release it descends from (OMN-18010)."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

# Location overrides would point git at another repository than ROOT.
_GIT_LOCATION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_uv_lock_pins_the_pyproject_version() -> None:
    with (ROOT / "uv.lock").open("rb") as fh:
        lock = tomllib.load(fh)
    pinned = [p["version"] for p in lock["package"] if p["name"] == "omnimarket"]
    assert pinned == [_pyproject_version()]


def test_version_is_ahead_of_every_reachable_release() -> None:
    """Strict mode (no --base) always enforces the version-ahead invariant."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV_VARS}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_release_identity.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
