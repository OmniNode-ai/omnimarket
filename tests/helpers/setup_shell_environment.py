# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Clear the inherited variables `onex setup`'s shell-safety step refuses (OMN-23413).

The root conftest binds BIFROST_CONTRACT_PATH and DELEGATION_ROUTING_TIERS_PATH
for every test. Setup's shell policy names both as legacy variables, so a setup
test that inherits them stops at shell-safety with action_required. A setup test
models a clean customer shell, so it removes every name the policy file names,
read from the policy itself so the tests cannot drift from the product. It also
puts this interpreter's own `onex` first on PATH: shell-safety flags any other
`onex` found first (a developer's installed tool, for one) as a conflict.
It runs the test from the home folder, where setup's workspace fallback
chain applies (AC19).
"""

from __future__ import annotations

import os
import platform
import re
import sys
from importlib import resources
from pathlib import Path

import pytest
import yaml


def isolate_setup_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = yaml.safe_load(
        resources.files("omnimarket.nodes.node_onex_setup_effect")
        .joinpath("shell_policy.yaml")
        .read_text(encoding="utf-8")
    )
    patterns = tuple(policy["legacy_variables"])
    for name in list(os.environ):
        if any(re.fullmatch(pattern, name) for pattern in patterns):
            monkeypatch.delenv(name, raising=False)
    own_bin = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", os.pathsep.join([own_bin, os.environ.get("PATH", "")]))
    # OMN-23413 AC19: setup takes the current folder as the workspace, except
    # from the home folder, where $OMNIBASE_PATH, $OMNI_HOME, the recorded root
    # or a prompt decide. Tests built on that chain run from the (fake) home.
    if Path.home().is_dir():
        monkeypatch.chdir(Path.home())


# `onex setup` preflight supports macOS only; on any other platform it reports
# unavailable and stops, so a full-walk subprocess test cannot pass there.
requires_supported_macos = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="onex setup preflight supports macOS only",
)
