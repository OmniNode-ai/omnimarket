# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the cost-and-throughput effect: the floor's documents on disk in, the served panel file out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .node_floor_cost_throughput_effect.test_handler_floor_cost_throughput_effect import (
    DATA,
    _floor_simple,
    _run,
)

pytestmark = pytest.mark.unit


def test_golden_chain_floor_documents_to_panel_file(tmp_path: Path) -> None:
    data = tmp_path / DATA
    data.mkdir(parents=True)
    (data / "floor-simple.json").write_text(
        json.dumps(_floor_simple("2026-11-20T14:59:00Z", 548, 1100)), encoding="utf-8"
    )
    panel = _run(tmp_path, "2026-11-20T15:00:00Z")
    assert panel["schema_id"] == "omnimarket.floor_cost_throughput.v1"
    assert panel["merges_per_hour"]["value"] == 7
    assert panel["merges_per_hour"]["source"]["read_at"] == "2026-11-20T14:56:30Z"
