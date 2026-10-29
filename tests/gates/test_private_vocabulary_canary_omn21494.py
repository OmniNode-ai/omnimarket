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
    return subprocess.run(
        [
            uv,
            "run",
            "--no-project",
            "--isolated",
            "--with",
            source,
            config["console_script"],
            "--name",
            config["vocabulary_name"],
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
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


def test_unset_variable_without_vocabulary_is_one_refusal_naming_setting(
    tmp_path: Path,
) -> None:
    config = _canary()
    result = _run(config, tmp_path)
    assert result.returncode != 0
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, result.stderr
    assert config["unset_setting"] in lines[0]
