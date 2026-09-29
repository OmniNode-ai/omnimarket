# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_lane_authority_is_packaged_once_without_a_stale_source_copy() -> None:
    repo = Path(__file__).resolve().parents[2]
    canonical = repo / "config" / "ci_bus_lanes.yaml"
    package_resource = repo / "src" / "omnimarket" / "config" / "ci_bus_lanes.yaml"
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    forced = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert not package_resource.exists()
    assert forced == {
        "config/ci_bus_lanes.yaml": "src/omnimarket/config/ci_bus_lanes.yaml"
    }
    assert canonical.is_file()
