"""The standing-rule admission nodes stay out of omnimarket until M0 passes."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"


def test_no_standing_rule_entry_points() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    entry_points = data["project"]["entry-points"]["onex.nodes"]
    assert [name for name in entry_points if "standing_rules" in name] == []


def test_no_standing_rule_node_directories() -> None:
    assert (
        sorted(p.name for p in NODES_DIR.glob("node_dispatch_standing_rules_*")) == []
    )
