# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Dependency Security Scan job must not fail every PR on a disabled graph."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/security-scan.yml"


def _review_step() -> dict[str, object]:
    data = yaml.safe_load(WORKFLOW.read_text())
    steps = data["jobs"]["dependency-scan"]["steps"]
    return next(s for s in steps if s.get("name") == "Run Dependency Review")


def test_dependency_review_runs_only_when_graph_is_enabled() -> None:
    cond = str(_review_step()["if"])
    assert "pull_request" in cond
    assert "vars.DEPENDENCY_GRAPH_ENABLED == 'true'" in cond


def test_dependency_review_action_pin_unchanged() -> None:
    uses = str(_review_step()["uses"])
    assert uses.startswith(
        "actions/dependency-review-action@2031cfc080254a8a887f58cffee85186f0e49e48"
    )
