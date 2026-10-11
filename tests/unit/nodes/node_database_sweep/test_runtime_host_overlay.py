# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime host the sweep probes comes from the deployment, never the package."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.node_overlay.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep import (
    _build_psql_argv,
    _resolve_pg_runtime_host,
)

NODE = "node_database_sweep"
OVERLAY_HOST = "overlay-host.example.invalid"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ONEX_DATABASE_SWEEP_RUNTIME_HOST",
        "ONEX_DATA_FLOW_RUNTIME_HOST",
        "ONEX_SKILL_OVERLAY_ROOTS",
    ):
        monkeypatch.delenv(var, raising=False)


def _overlay_root(tmp_path: Path, content: object) -> Path:
    path = tmp_path / "overlays" / NODE / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return tmp_path / "overlays"


def test_overlay_supplies_the_runtime_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_overlay_root(tmp_path, {"runtime_host": OVERLAY_HOST})),
    )
    assert _resolve_pg_runtime_host() == OVERLAY_HOST
    argv = _build_psql_argv("SELECT 1;", "omnidash_analytics")
    assert argv[0] == "ssh"
    assert argv[1].endswith(f"@{OVERLAY_HOST}")


def test_environment_beats_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_overlay_root(tmp_path, {"runtime_host": OVERLAY_HOST})),
    )
    monkeypatch.setenv("ONEX_DATA_FLOW_RUNTIME_HOST", "env-host.example.invalid")
    assert _resolve_pg_runtime_host() == "env-host.example.invalid"
    monkeypatch.setenv("ONEX_DATABASE_SWEEP_RUNTIME_HOST", "")
    assert _resolve_pg_runtime_host() == ""


def test_without_an_overlay_the_neutral_default_is_the_local_docker_lane() -> None:
    assert _resolve_pg_runtime_host() == ""
    argv = _build_psql_argv("SELECT 1;", "omnidash_analytics")
    assert argv[:2] == ["docker", "exec"]
    assert "ssh" not in argv


def test_overlay_with_an_unknown_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_overlay_root(tmp_path, {"runtime_hots": OVERLAY_HOST})),
    )
    with pytest.raises(NodeOverlayError, match="runtime_hots"):
        _resolve_pg_runtime_host()
