# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Knowledge-base and snapshot shell cases from the remote-lane source tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lane_host_discovery_compute.handlers.handler_lane_host_discovery import (
    HandlerLaneHostDiscovery,
    kb_lines,
    prrp_snapshot_lines,
)
from omnimarket.nodes.node_lane_host_discovery_compute.models.model_lane_host_discovery import (
    PRRP_SNAPSHOT_DESTINATION,
    ModelLaneHostDiscoveryRequest,
)

pytestmark = pytest.mark.unit


def shell(
    tmp_path: Path,
    lines: tuple[str, ...],
    values: dict[str, str] | None = None,
    profile: str = "",
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # The source uses a login shell to query a profile. Intercept only that query,
    # so tests exercise the rendered bash without reading the operator's profile.
    body = "\n".join([f"bash() {{ {profile or ':'}; }}", *lines])
    return subprocess.run(
        ["bash", "-c", body],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "EXAMPLE_REGISTRY": str(tmp_path / "registry"),
            **(values or {}),
        },
        timeout=30,
        check=False,
    )


def knowledge_lines() -> tuple[str, ...]:
    return (
        *kb_lines("kb_repo_example", "KB_EXAMPLE_PATH", "EXAMPLE_REGISTRY"),
        'echo "FOUND=${KB_EXAMPLE_PATH:-UNSET}"',
    )


def test_kb_resolution_takes_the_host_env_value_first(tmp_path: Path) -> None:
    clone = tmp_path / "clone with spaces"
    (clone / ".git").mkdir(parents=True)
    sibling = tmp_path / "registry" / "kb_repo_example"
    (sibling / ".git").mkdir(parents=True)
    done = shell(tmp_path, knowledge_lines(), {"KB_EXAMPLE_PATH": str(clone)})
    assert done.returncode == 0, done.stderr
    assert f"REMOTE-LANE-KB-PATH {clone}" in done.stdout
    assert f"FOUND={clone}" in done.stdout


def test_kb_resolution_absent_when_nothing_names_a_clone(tmp_path: Path) -> None:
    done = shell(
        tmp_path, knowledge_lines(), {"KB_EXAMPLE_PATH": str(tmp_path / "missing")}
    )
    assert done.returncode == 0, done.stderr
    assert "REMOTE-LANE-KB-ABSENT no knowledge-base clone" in done.stdout
    assert "FOUND=UNSET" in done.stdout


def test_kb_resolution_uses_registry_clone(tmp_path: Path) -> None:
    clone = tmp_path / "registry" / "kb_repo_example"
    (clone / ".git").mkdir(parents=True)
    done = shell(tmp_path, knowledge_lines())
    assert f"REMOTE-LANE-KB-PATH {clone}" in done.stdout
    assert f"FOUND={clone}" in done.stdout


def test_kb_resolution_uses_login_profile_before_registry(tmp_path: Path) -> None:
    clone = tmp_path / "profile-clone"
    (clone / ".git").mkdir(parents=True)
    done = shell(tmp_path, knowledge_lines(), profile=f"printf %s '{clone}'")
    assert f"FOUND={clone}" in done.stdout


def snapshot_lines() -> tuple[str, ...]:
    return (
        *prrp_snapshot_lines(PRRP_SNAPSHOT_DESTINATION),
        'echo "SNAP=${PRRP_VARIABLES_SNAPSHOT:-UNSET}"',
    )


def test_the_published_snapshot_is_exported(tmp_path: Path) -> None:
    target = tmp_path / "home" / PRRP_SNAPSHOT_DESTINATION
    target.parent.mkdir(parents=True)
    target.write_text('{"schema": 1}')
    done = shell(tmp_path, snapshot_lines())
    assert done.returncode == 0, done.stderr
    assert f"SNAP={target}" in done.stdout
    assert f"REMOTE-LANE-PRRP-SNAPSHOT {target}" in done.stdout


def test_the_host_env_file_value_wins(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere.json"
    other.write_text('{"schema": 1}')
    target = tmp_path / "home" / PRRP_SNAPSHOT_DESTINATION
    target.parent.mkdir(parents=True)
    target.write_text('{"schema": 1}')
    # The runner has already loaded the host file when this node's lines run.
    done = shell(tmp_path, snapshot_lines(), {"PRRP_VARIABLES_SNAPSHOT": str(other)})
    assert f"SNAP={other}" in done.stdout, done.stdout + done.stderr


def test_no_snapshot_leaves_the_variable_unset_and_says_so(tmp_path: Path) -> None:
    done = shell(
        tmp_path,
        snapshot_lines(),
        {"PRRP_VARIABLES_SNAPSHOT": str(tmp_path / "missing.json")},
    )
    assert "SNAP=UNSET" in done.stdout, done.stdout + done.stderr
    assert "REMOTE-LANE-PRRP-SNAPSHOT-ABSENT" in done.stdout
    assert "onex-prrp-snapshot-publish" in done.stdout


def test_snapshot_profile_precedes_published_file_and_content_is_not_read(
    tmp_path: Path,
) -> None:
    other = tmp_path / "profile.json"
    other.write_text("not JSON; the hook judges this")
    done = shell(tmp_path, snapshot_lines(), profile=f"printf %s '{other}'")
    assert f"SNAP={other}" in done.stdout


@pytest.mark.parametrize(
    ("values", "setting"),
    [
        ({}, "knowledge_base"),
        ({"knowledge_base": "kb_repo_example"}, "knowledge_base_home_env"),
    ],
)
def test_missing_overlay_names_the_setting(
    values: dict[str, str], setting: str
) -> None:
    with pytest.raises(NodeOverlayError, match=setting):
        HandlerLaneHostDiscovery().handle(
            ModelLaneHostDiscoveryRequest(environ={}, **values)
        )


def test_overlay_resolves_both_settings(tmp_path: Path) -> None:
    overlay = tmp_path / "node_lane_host_discovery_compute" / "overlay.yaml"
    overlay.parent.mkdir()
    overlay.write_text(
        "knowledge_base: kb_repo_example\nknowledge_base_home_env: KB_EXAMPLE_PATH\n"
    )
    result = HandlerLaneHostDiscovery().handle(
        ModelLaneHostDiscoveryRequest(
            environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
        )
    )
    assert result.kb_lines == kb_lines(
        "kb_repo_example", "KB_EXAMPLE_PATH", "ONEX_REGISTRY_ROOT"
    )
    assert result.prrp_snapshot_lines == prrp_snapshot_lines(PRRP_SNAPSHOT_DESTINATION)


def test_environment_overrides_overlay_and_explicit_values_win(tmp_path: Path) -> None:
    overlay = tmp_path / "node_lane_host_discovery_compute" / "overlay.yaml"
    overlay.parent.mkdir()
    overlay.write_text(
        "knowledge_base: other_repo\nknowledge_base_home_env: OTHER_PATH\n"
    )
    environ = {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path),
        "ONEX_KNOWLEDGE_BASE_REPO": "env_repo",
        "ONEX_KNOWLEDGE_BASE_HOME_ENV": "ENV_PATH",
    }
    result = HandlerLaneHostDiscovery().handle(
        ModelLaneHostDiscoveryRequest(environ=environ)
    )
    assert result.kb_lines == kb_lines("env_repo", "ENV_PATH")
    result = HandlerLaneHostDiscovery().handle(
        ModelLaneHostDiscoveryRequest(
            knowledge_base="explicit_repo",
            knowledge_base_home_env="EXPLICIT_PATH",
            environ=environ,
        )
    )
    assert result.kb_lines == kb_lines("explicit_repo", "EXPLICIT_PATH")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("knowledge_base", "bad/name"),
        ("knowledge_base", ""),
        ("knowledge_base", "repo;exit"),
        ("knowledge_base_home_env", "BAD-NAME"),
        ("knowledge_base_home_env", ""),
        ("registry_root_var", "1ROOT"),
        ("registry_root_var", "ROOT;exit"),
    ],
)
def test_shell_settings_are_validated(field: str, value: str) -> None:
    values = {
        "knowledge_base": "kb_repo_example",
        "knowledge_base_home_env": "KB_EXAMPLE_PATH",
        "registry_root_var": "EXAMPLE_REGISTRY",
        field: value,
    }
    with pytest.raises(NodeOverlayError, match=field):
        HandlerLaneHostDiscovery().handle(
            ModelLaneHostDiscoveryRequest(environ={}, **values)
        )
