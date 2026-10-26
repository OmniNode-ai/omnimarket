# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The eight session-start nodes are present, free of the old registry-root variable."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

NODES_ROOT = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"
SESSION_START_NODES = (
    "node_adoption_recovery_effect",
    "node_cause_claim_ownership_effect",
    "node_host_pool_alarm_effect",
    "node_script_node_src_compute",
    "node_controller_fix_plan_compute",
    "node_operator_waiting_tick_effect",
    "node_green_draft_clock_compute",
    "node_green_controller_read_compute",
)
SOURCE_FILES = ("contract.yaml", "handler.py", "models.py", "node.py")
OLD_REGISTRY_ROOT_VARIABLE = "OMNI" + "_HOME"


def test_the_eight_nodes_carry_their_32_source_files() -> None:
    expected = [
        NODES_ROOT / node / name
        for node in SESSION_START_NODES
        for name in SOURCE_FILES
    ]
    assert len(expected) == 32
    missing = [
        str(path.relative_to(NODES_ROOT)) for path in expected if not path.is_file()
    ]
    assert missing == []


def test_no_file_of_the_eight_nodes_names_the_old_registry_root_variable() -> None:
    offenders: list[str] = []
    for node in SESSION_START_NODES:
        node_dir = NODES_ROOT / node
        assert node_dir.is_dir(), f"{node} is missing"
        for path in sorted(node_dir.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if OLD_REGISTRY_ROOT_VARIABLE in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(NODES_ROOT)))
    assert offenders == []
