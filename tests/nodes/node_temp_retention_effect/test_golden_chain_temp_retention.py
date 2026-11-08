# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_temp_retention_effect ages out an unheld temp entry through the node entry point."""

from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    HandlerTempRetention,
    ProcessReferences,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention_request import (
    ModelTempRetentionRequest,
)
from omnimarket.nodes.node_temp_retention_effect.node import NodeTempRetentionEffect


def test_temp_retention_golden_chain_removes_the_old_unheld_entry_and_keeps_the_held_one(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    temp = runs / ".tmp"
    old = temp / "old-unheld"
    held = temp / "old-held"
    for entry in (old, held):
        entry.mkdir(parents=True)
        (entry / "file").write_text("x")
    now = time.time()
    stamp = now - 72 * 3600
    for entry in (old, held):
        os.utime(entry / "file", (stamp, stamp))
        os.utime(entry, (stamp, stamp))
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {
                        "name": "pool-a",
                        "scheduler": "systemd-user",
                        "windows": {
                            "retain_hours": 12,
                            "pressure_percent": 80,
                            "pressure_retain_hours": 2,
                        },
                        "temp_roots": [{"path": str(temp)}],
                    }
                ]
            }
        )
    )
    node = NodeTempRetentionEffect()
    node.handler = HandlerTempRetention(
        now=lambda: now,
        references=lambda: ProcessReferences(directories=[held], blobs=[]),
    )
    contract = yaml.safe_load(node.contract_path.read_text())
    assert contract["name"] == "node_temp_retention_effect"

    result = node.handle(
        ModelTempRetentionRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    assert result.removed == (str(old),)
    assert result.busy == (str(held),)
    assert not old.exists()
    assert held.exists()

    again = node.handle(
        ModelTempRetentionRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    assert again.removed_count == 0
