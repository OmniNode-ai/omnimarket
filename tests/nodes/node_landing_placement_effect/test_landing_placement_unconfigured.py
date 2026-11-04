# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pool read with nothing configured refuses (exit 75, naming ``lab_hosts.hosts``); the explicit empty pool runs local-only."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_landing_placement_effect.handlers import (
    handler_landing_placement as lp,
)
from omnimarket.nodes.node_landing_placement_effect.models import (
    ModelLandingPlacementRequest,
)

PROBE = "PROBE 8 0.50 33554432\nCOAUTHOR false\nLOGIN 1 1"


def _bare_environ(tmp_path: Path) -> dict[str, str]:
    """An overlay root that holds nothing, with the three variables that could supply a host table unset."""
    root = tmp_path / "empty-overlay-root"
    root.mkdir()
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _probe(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, PROBE, "")


def test_landing_placement_unconfigured_pool_read_exits_75_naming_lab_hosts_hosts(
    tmp_path: Path,
) -> None:
    result = lp.HandlerLandingPlacement(capture=_probe).handle(
        ModelLandingPlacementRequest(environ=_bare_environ(tmp_path))
    )
    assert result.exit_code == lp.EXIT_NO_POOL == 75
    assert "lab_hosts.hosts" in result.message
    assert result.hosts == ()


def test_landing_placement_unconfigured_load_pool_raises_naming_the_key(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match=r"lab_hosts\.hosts"):
        lp.load_pool(_bare_environ(tmp_path))


def test_landing_placement_unconfigured_empty_pool_switch_runs_local_only(
    tmp_path: Path,
) -> None:
    environ = {**_bare_environ(tmp_path), "LANDING_POOL_HOSTS": "[]"}
    result = lp.HandlerLandingPlacement(capture=_probe).handle(
        ModelLandingPlacementRequest(environ=environ)
    )
    assert result.exit_code == 0
    assert result.hosts == ()
    assert result.pool_source == "LANDING_POOL_HOSTS"


def test_landing_placement_unconfigured_one_local_row_reads_that_host_only(
    tmp_path: Path,
) -> None:
    row = '[{"name": "host-local", "local": true, "engines": ["claude_sonnet"], "cap": 4}]'
    environ = {**_bare_environ(tmp_path), "LANDING_POOL_HOSTS": row}
    seen: list[list[str]] = []

    def capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, PROBE, "")

    result = lp.HandlerLandingPlacement(capture=capture).handle(
        ModelLandingPlacementRequest(environ=environ)
    )
    assert result.exit_code == 0
    assert [h.name for h in result.hosts] == ["host-local"]
    assert result.hosts[0].engines == ("claude_sonnet",)
    assert all(argv[0] != "ssh" for argv in seen), (
        "a local-only pool reads no remote host"
    )


def test_landing_placement_unconfigured_registry_roots_refuse_naming_the_key(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match=r"lab_hosts\.registry_root_candidates"):
        lp.registry_root_candidates(_bare_environ(tmp_path))
