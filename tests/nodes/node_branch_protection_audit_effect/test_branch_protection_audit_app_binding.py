# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every required status check is bound to a reporting App.

Ported from onex_change_control ``tests/test_audit_branch_protection_app_binding.py``.

``required_status_checks.checks[].app_id`` names the App whose check runs
satisfy that context. A null ``app_id`` is GitHub's "Any source": any App or
Action with ``checks:write`` can satisfy it. The unbound case reproduces the
live shape that motivated the check (one context with ``app_id: null`` among
bound ones); its bound partner stops the assertion being satisfied by deleting
it; the release-synced main case pins that an empty ``checks[]`` has nothing to
be unbound.
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

REPO = "omniclaude"

_CONTEXTS = [
    "CI Summary",
    "verify / verify",
    "Lane Identity Gate",
    "advisory-job-gate / advisory-job-gate",
]


def _dev(unbound: str | None) -> dict[str, object]:
    return {
        "required_status_checks": {
            "strict": False,
            "contexts": list(_CONTEXTS),
            "checks": [
                {"context": c, "app_id": None if c == unbound else 15368}
                for c in _CONTEXTS
            ],
        },
        "enforce_admins": {"enabled": True},
    }


_MAIN_RELEASE_SYNCED: dict[str, object] = {
    "required_status_checks": {"strict": False, "contexts": [], "checks": []},
    "enforce_admins": {"enabled": True},
}


def _fixtures(dev_protection: dict[str, object]) -> dict[str, object]:
    base = f"repos/{ORG}/{REPO}"
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


def test_dev_with_unbound_required_check_fails() -> None:
    result = run_audit(
        REPO,
        _fixtures(_dev("advisory-job-gate / advisory-job-gate")),
        branches="dev",
    )
    assert result.exit_code == 1, result.report
    assert "advisory-job-gate / advisory-job-gate" in result.report
    assert "app_id=null" in result.report
    assert "Any source" in result.report


def test_dev_with_all_checks_bound_passes() -> None:
    result = run_audit(REPO, _fixtures(_dev(None)), branches="dev")
    assert result.exit_code == 0, result.report
    assert "every required status check is bound to a reporting App" in result.report


def test_release_synced_main_is_unaffected() -> None:
    result = run_audit(REPO, _fixtures(_dev(None)), branches="main")
    assert result.exit_code == 0, result.report
    assert "app_id" not in result.report
