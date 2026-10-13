# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Lane Identity Gate is a required status check on dev, for the listed repos.

Ported from onex_change_control ``tests/test_audit_branch_protection_lane_identity.py``.

A gate that reports and cannot block is advisory detection, so on the listed
repository's dev the context is asserted, not noted. It is asserted on dev only
(that repository's main is release-synced, empty by design) and only for the
listed repositories, because asking a repository for a context it cannot
produce wedges every pull request there on a check nothing will post.
"""

from __future__ import annotations

import pytest

from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_RULES,
    ORG,
    REPO_SETTINGS,
    RULESET_MERGE_QUEUE_DISABLED,
    RULESET_RESTRICTING_MAIN,
    run_audit,
)

pytestmark = pytest.mark.unit

LANE_IDENTITY_REPO = "omniclaude"
OTHER_REPO = "omnibase_core"

_DEV_WITHOUT_LANE_IDENTITY: dict[str, object] = {
    "required_status_checks": {
        "strict": False,
        "contexts": ["CI Summary", "verify / verify"],
        "checks": [
            {"context": "CI Summary", "app_id": 15368},
            {"context": "verify / verify", "app_id": 15368},
        ],
    },
    "enforce_admins": {"enabled": True},
}
_DEV_WITH_LANE_IDENTITY: dict[str, object] = {
    "required_status_checks": {
        "strict": False,
        "contexts": ["CI Summary", "verify / verify", "Lane Identity Gate"],
        "checks": [
            {"context": "CI Summary", "app_id": 15368},
            {"context": "verify / verify", "app_id": 15368},
            {"context": "Lane Identity Gate", "app_id": 15368},
        ],
    },
    "enforce_admins": {"enabled": True},
}
_MAIN_RELEASE_SYNCED: dict[str, object] = {
    "required_status_checks": {"strict": False, "contexts": [], "checks": []},
    "enforce_admins": {"enabled": True},
}


def _fixtures(repo: str, dev_protection: dict[str, object]) -> dict[str, object]:
    base = f"repos/{ORG}/{repo}"
    rulesets = [RULESET_MERGE_QUEUE_DISABLED, RULESET_RESTRICTING_MAIN]
    fx: dict[str, object] = {
        "graphql": GQL_RULES,
        f"{base}/branches/main/protection": _MAIN_RELEASE_SYNCED,
        f"{base}/branches/dev/protection": dev_protection,
        base: REPO_SETTINGS,
        f"{base}/rulesets": rulesets,
    }
    for rs in rulesets:
        fx[f"{base}/rulesets/{rs['id']}"] = rs
    return fx


def test_dev_with_lane_identity_gate_passes() -> None:
    result = run_audit(
        LANE_IDENTITY_REPO,
        _fixtures(LANE_IDENTITY_REPO, _DEV_WITH_LANE_IDENTITY),
        branches="dev",
    )
    assert result.exit_code == 0, result.report
    assert '"Lane Identity Gate" is a required status check' in result.report


def test_dev_missing_lane_identity_gate_fails() -> None:
    result = run_audit(
        LANE_IDENTITY_REPO,
        _fixtures(LANE_IDENTITY_REPO, _DEV_WITHOUT_LANE_IDENTITY),
        branches="dev",
    )
    assert result.exit_code == 1, result.report
    assert '"Lane Identity Gate" not found in required status checks' in result.report
    assert "reports but cannot block" in result.report


def test_lane_identity_gate_is_not_asserted_on_main() -> None:
    result = run_audit(
        LANE_IDENTITY_REPO,
        _fixtures(LANE_IDENTITY_REPO, _DEV_WITH_LANE_IDENTITY),
        branches="main",
    )
    assert result.exit_code == 0, result.report
    assert "Lane Identity Gate" not in result.report


def test_a_repo_outside_the_list_is_not_asserted() -> None:
    result = run_audit(
        OTHER_REPO,
        _fixtures(OTHER_REPO, _DEV_WITHOUT_LANE_IDENTITY),
        branches="dev",
    )
    assert result.exit_code == 0, result.report
    assert "Lane Identity Gate" not in result.report
