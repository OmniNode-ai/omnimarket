# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the overlay file and the node."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from omnimarket.nodes.node_hook_tool_outcome_compute.models import (
    ModelToolOutcomeRules,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.overlay import (
    load_outcome_rules,
)

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_tool_outcome_compute"
    / "overlay.example.yaml"
)


def test_overlay_example_validates_as_model_tool_outcome_rules() -> None:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    ModelToolOutcomeRules.model_validate(raw)


def test_load_outcome_rules_reads_the_overlay_file(tmp_path: Path) -> None:
    node_dir = tmp_path / "node_hook_tool_outcome_compute"
    node_dir.mkdir()
    shutil.copy(EXAMPLE, node_dir / "overlay.yaml")
    rules = load_outcome_rules(environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert len(rules.refusal_rules) == 3


def test_every_rule_name_cited_in_the_golden_table_exists_in_the_file() -> None:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    names = {rule["name"] for rule in raw["refusal_rules"]}
    for cited in (
        "refused_component_line",
        "refused_ticket_held",
        "status_refused_json",
    ):
        assert cited in names, f"rule {cited!r} missing from overlay.example.yaml"
