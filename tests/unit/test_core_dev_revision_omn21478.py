# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read back the requested core dev revision through Market's installed runtime."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE_TAG = "dev-20261031234507-191fea0c1d40"
CORE_COMMIT = "191fea0c1d407149e6d071b5c6d8a4887b6c64ee"
CORE_REPO = "https://github.com/OmniNode-ai/omnibase_core.git"
pytestmark = pytest.mark.unit


def test_requested_core_dev_revision_is_declared_and_locked() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["tool"]["uv"]["sources"]["omnibase-core"] == {
        "git": CORE_REPO,
        "tag": CORE_TAG,
    }
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    (core,) = [p for p in lock["package"] if p["name"] == "omnibase-core"]
    assert core["source"]["git"] == f"{CORE_REPO}?tag={CORE_TAG}#{CORE_COMMIT}"


def test_installed_core_revision_loads_market_runtime_consumers() -> None:
    provenance = importlib.metadata.distribution("omnibase-core").read_text(
        "direct_url.json"
    )
    assert provenance is not None, "The installed core must record its Git provenance"
    installed = json.loads(provenance)
    assert installed["url"] == CORE_REPO
    assert installed["vcs_info"]["commit_id"] == CORE_COMMIT
    for module in (
        "omnimarket.nodes.node_linear_triage.services.done_write_receipt_gate",
        "omnimarket.nodes.node_duplication_sweep.handlers.handler_duplication_sweep",
        "omnibase_core.nodes.node_contract_walker_check_compute.runtime_contract_walker_check",
    ):
        importlib.import_module(module)
