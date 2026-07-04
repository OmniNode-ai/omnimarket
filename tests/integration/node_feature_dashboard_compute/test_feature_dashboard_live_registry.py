# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live-registry regression pins for node_feature_dashboard_compute.

Defect class being pinned: feature_dashboard reported 0% coverage (or an
empty audit) for skills that demonstrably work, because its discovery layer
scanned stale SKILL.md directory layouts instead of the live `onex skill`
dispatch registry. These tests run the handler with NO repo_root override —
the exact invocation path `onex skill feature_dashboard` takes — against the
installed ``omnibase_infra`` ``skill_mapping.yaml`` and the installed
``onex.nodes`` entry points, and pin that known-good skills register as
covered.

If discovery ever regresses to a surface where dispatch-registered,
working skills score 0 (or the audit goes empty), these tests fail.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from omnimarket.nodes.node_feature_dashboard_compute.handlers.handler_feature_dashboard_compute import (
    GAP_REGISTRY_INCONSISTENCY,
    HandlerFeatureDashboardCompute,
    _load_skill_registry,
)
from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_request import (
    ModelFeatureDashboardRequest,
)

# The skills verified working in the 2026-07-03 operator adoption baseline
# ("fold-in-now"). Not all are guaranteed present in every installed
# omnibase_infra registry revision (the registry is data and evolves), so
# each is asserted only when present — but the two named in the defect
# evidence (merge_sweep, contract_sweep) are hard-required.
FOLD_IN_NOW_SKILLS = (
    "merge_sweep",
    "pipeline_fill",
    "aislop_sweep",
    "golden_chain_sweep",
    "contract_sweep",
    "dod_sweep",
    "dod_verify",
)
HARD_REQUIRED_SKILLS = ("merge_sweep", "contract_sweep")


@pytest.mark.integration
def test_live_registry_discovery_is_non_empty() -> None:
    """The default (no repo_root) audit must discover the live registry."""
    result = HandlerFeatureDashboardCompute().handle(ModelFeatureDashboardRequest())

    registry = _load_skill_registry(None)
    assert result.skills_audited == len(registry)
    assert result.skills_audited > 0
    assert result.status != "empty"


@pytest.mark.integration
def test_known_good_skills_register_as_covered() -> None:
    """No false 0% rows for skills with working receipts (OMN-13922 DoD)."""
    result = HandlerFeatureDashboardCompute().handle(ModelFeatureDashboardRequest())

    for skill in HARD_REQUIRED_SKILLS:
        assert skill in result.coverage_report, (
            f"{skill} missing from audit — discovery no longer sees the "
            "live dispatch registry"
        )

    present = [s for s in FOLD_IN_NOW_SKILLS if s in result.coverage_report]
    assert present, "none of the fold-in-now skills were discovered"
    for skill in present:
        row = cast(dict[str, Any], result.coverage_report[skill])
        assert row["registered"] is True
        assert row["coverage_score"] > 0.0, (skill, row)
        assert row["checks"].get("backing_node") is True, (skill, row)

    inconsistencies = [
        gap
        for gap in result.gaps
        if gap["check_type"] == GAP_REGISTRY_INCONSISTENCY and gap["skill"] in present
    ]
    assert not inconsistencies, inconsistencies
