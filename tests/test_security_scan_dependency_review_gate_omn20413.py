# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dependency-review step runs only when the repository's graph is enabled."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/security-scan.yml"


def _steps() -> list[dict[str, object]]:
    data = yaml.safe_load(WORKFLOW.read_text())
    return list(data["jobs"]["dependency-scan"]["steps"])


def test_dependency_review_step_is_gated_on_graph_variable() -> None:
    review = [
        s for s in _steps() if "dependency-review-action" in str(s.get("uses", ""))
    ]
    assert len(review) == 1
    cond = str(review[0]["if"])
    assert "github.event_name == 'pull_request'" in cond
    assert "vars.DEPENDENCY_GRAPH_ENABLED == 'true'" in cond


def test_safety_check_still_runs_ungated() -> None:
    safety = [s for s in _steps() if s.get("name") == "Run Safety Check"]
    assert len(safety) == 1
    assert "if" not in safety[0]
