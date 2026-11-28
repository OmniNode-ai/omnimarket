# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C30 scratch profile is hermetic: a host's workspace binding never reaches the walked `onex delegate` (OMN-19966).

On a lab host the unit's environment names the registry root and the workspace config root. A scratch profile that
inherits them resolves the walked delegation to the workspace's kafka bus and is refused for want of a bus identity,
where the cloud runner this probe came from held no such binding and resolved the shipped in-memory default.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.handlers import (
    probe_c30 as c30,
)

pytestmark = pytest.mark.unit

BINDING_VARIABLES = (
    "OMNI_HOME",
    "OMNIBASE_PATH",
    "ONEX_WORKSPACE_CONFIG_ROOT",
    "ONEX_CONTRACTS_DIR",
    "ONEX_SKILL_OVERLAY_ROOTS",
)


def _walk_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    for name in BINDING_VARIABLES:
        monkeypatch.setenv(name, "/bound/by/the/host")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen: list[dict[str, str]] = []

    def record(argv, timeout, *, env, cwd):
        seen.append(dict(env))
        return 1, "", "stop after the first command"

    c30.walk(
        "published",
        "OmniNode-ai/omniclaude",
        "onex@omninode-tools",
        tmp_path,
        "nonce",
        endpoint_url="http://127.0.0.1:1",
        run_cmd=record,
    )
    assert seen, "the walk ran no command"
    return seen[0]


@pytest.mark.parametrize("name", BINDING_VARIABLES)
def test_the_walk_env_drops_the_hosts_workspace_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    assert name not in _walk_env(monkeypatch, tmp_path)


def test_the_walk_env_resolves_onex_to_the_running_interpreters_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _walk_env(monkeypatch, tmp_path)["PATH"].split(":")[0]
    assert first == str(Path(sys.executable).parent)
