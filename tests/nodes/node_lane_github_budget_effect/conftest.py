# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-lane allowance is an overlay value (OMN-23832): every test here reads the same synthetic one."""

from __future__ import annotations

import pytest
import yaml


@pytest.fixture(autouse=True)
def budget_overlay(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("budget-overlay") / "overlay.yaml"
    path.write_text(yaml.safe_dump({"gateway_cache_misses_per_lane": 8}))
    monkeypatch.setenv("ONEX_LANE_GITHUB_BUDGET_OVERLAY", str(path))
