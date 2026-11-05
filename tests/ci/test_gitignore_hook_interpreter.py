# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gitignore hook runs on a commit host with no bare python command."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("altered", [False, True])
def test_gitignore_hook_without_python_on_path(tmp_path: Path, altered: bool) -> None:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "validate-gitignore-baseline"
    )
    # The recorded commit host has uv and python3, but no `python`. Exercise
    # the actual configured command in that environment, including its validator.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = shutil.which("uv")
    assert uv is not None, "uv is the repository's required package runner"
    (bin_dir / "uv").symlink_to(uv)
    (bin_dir / "python3").symlink_to(sys.executable)
    target = tmp_path / "target"
    target.mkdir()
    text = (REPO_ROOT / ".gitignore").read_text()
    if altered:
        text = text.replace("# === onex-managed: universal ===", "# altered")
    (target / ".gitignore").write_text(text)
    (target / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "0.0.0"\n'
    )
    env = {**os.environ, "PATH": str(bin_dir), "UV_PYTHON": sys.executable}
    assert shutil.which("python", path=env["PATH"]) is None
    try:
        result = subprocess.run(
            [*shlex.split(hook["entry"]), str(target)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except FileNotFoundError as exc:
        pytest.fail(f"configured gitignore hook needs an absent interpreter: {exc}")
    output = result.stdout + result.stderr
    if altered:
        assert result.returncode == 1, output
        assert "managed block 'universal' is missing or altered" in output
    else:
        assert result.returncode == 0, output
