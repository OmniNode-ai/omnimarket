# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21494 AC3 canary: the hygiene vocabulary resolves with the variable unset.

The test runs the ``resolve-private-vocabulary`` console script of omnibase_core,
built at the dev-merge tag named in ``canary.yaml``, with ``OMNI_HYGIENE_VOCAB_PATH``
removed from the environment and the vocabulary placed at a workspace config root.
It asserts the resolved path (the case the hygiene gate refused on 2026-10-12) and
plants the violation: with no vocabulary anywhere the script exits nonzero with one
refusal line that names the setting.

Scope: this proves the core resolution that the hygiene gate consumer reads through.
The omniclaude gate script does not yet read through the handler; that wiring is the
omniclaude follow-up named in the OMN-21494 contract, not part of this canary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

_DATA = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "private_vocabulary_canary"
    / "canary.yaml"
)


def _canary() -> dict[str, str]:
    loaded = yaml.safe_load(_DATA.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return {str(k): str(v) for k, v in loaded.items()}


def _run(config: dict[str, str], root: Path) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is required to build omnibase_core at the dev-merge tag")
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            config["unset_setting"],
            config["config_root_setting"],
            "PYTHONPATH",
            # uv warns on stderr when UV_NO_SYNC meets --no-project, which
            # would add a second line to the one-refusal assertion.
            "UV_NO_SYNC",
        }
    }
    env[config["config_root_setting"]] = str(root)
    source = f"omnibase-core @ git+{config['core_repository']}@{config['core_dev_merge_tag']}"
    # Provision the pinned environment separately: uv's VCS/install diagnostics
    # are not the console script's diagnostic protocol.
    scripts = root / "canary-venv" / "bin"
    for command in (
        [uv, "venv", "--python", sys.executable, str(scripts.parent)],
        [uv, "pip", "install", "--python", str(scripts / "python"), source],
    ):
        provision = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        sys.stderr.write(provision.stderr)
        provision.check_returncode()
    script = scripts / config["console_script"]
    return subprocess.run(
        [str(script), "--name", config["vocabulary_name"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_unset_variable_resolves_vocabulary_at_config_root(tmp_path: Path) -> None:
    config = _canary()
    vocabulary = tmp_path / config["config_root_relative_path"]
    vocabulary.parent.mkdir(parents=True)
    vocabulary.write_text(
        "private_repo_names: []\nprivate_repo_names_resolved_at: '2099-01-01T00:00:00Z'\n",
        encoding="utf-8",
    )
    result = _run(config, tmp_path)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == vocabulary.resolve()


@pytest.mark.parametrize(
    "dependency_diagnostics", [False, True], ids=["warm", "uv-update"]
)
def test_unset_variable_without_vocabulary_is_one_refusal_naming_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dependency_diagnostics: bool
) -> None:
    if dependency_diagnostics:
        uv = shutil.which("uv")
        assert uv is not None
        wrappers = tmp_path / "bin"
        wrappers.mkdir()
        wrapper = wrappers / "uv"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "print('   Updating pinned dependency', file=sys.stderr)\n"
            "print('    Updated pinned dependency', file=sys.stderr)\n"
            f"os.execv({uv!r}, [{uv!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{wrappers}{os.pathsep}{os.environ['PATH']}")
    config = _canary()
    result = _run(config, tmp_path)
    assert result.returncode != 0
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, result.stderr
    assert config["unset_setting"] in lines[0]
