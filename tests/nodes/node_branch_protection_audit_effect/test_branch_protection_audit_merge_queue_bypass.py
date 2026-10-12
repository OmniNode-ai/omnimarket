# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An enforced merge-queue ruleset carries no bypass actor.

Ported from onex_change_control ``tests/test_audit_branch_protection_merge_queue_bypass.py``.

A bypass actor on the merge-queue ruleset lets that identity merge outside the
queue. A ruleset read with a token that cannot administer the repository comes
back with no ``bypass_actors`` key, which looks like a clean ruleset to a check
that treats absence as empty, so an unreadable list fails as well:

* one bypass actor on an active merge-queue ruleset: FAIL, names the actor;
* no ``bypass_actors`` key (unreadable): FAIL, says unreadable;
* the detail read itself fails: FAIL;
* an empty bypass list: PASS;
* a disabled merge-queue ruleset with a bypass actor: not judged, PASS.
"""

from __future__ import annotations

import pytest

from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_RULES,
    ORG,
    PROTECTION_CI_SUMMARY,
    REPO_SETTINGS,
    RULESET_RESTRICTING_MAIN,
    run_audit,
)

pytestmark = pytest.mark.unit

REPO = "omnibase_infra"

_ADMIN_ROLE_BYPASS = {
    "actor_id": 5,
    "actor_type": "RepositoryRole",
    "bypass_mode": "pull_request",
}


def _queue_ruleset(**overrides: object) -> dict[str, object]:
    ruleset: dict[str, object] = {
        "id": 13269702,
        "name": "Merge Queue",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [{"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}}],
        "bypass_actors": [],
    }
    ruleset.update(overrides)
    return ruleset


def _unreadable_queue_ruleset() -> dict[str, object]:
    ruleset = _queue_ruleset()
    del ruleset["bypass_actors"]
    return ruleset


def _fixtures(
    queue_ruleset: dict[str, object], *, detail_readable: bool = True
) -> dict[str, object]:
    base = f"repos/{ORG}/{REPO}"
    rulesets = [queue_ruleset, RULESET_RESTRICTING_MAIN]
    fx: dict[str, object] = {
        "graphql": GQL_RULES,
        f"{base}/branches/dev/protection": PROTECTION_CI_SUMMARY,
        base: REPO_SETTINGS,
        # The list endpoint carries no rules and no bypass list.
        f"{base}/rulesets": [
            {k: v for k, v in rs.items() if k not in ("rules", "bypass_actors")}
            for rs in rulesets
        ],
        f"{base}/rulesets/{RULESET_RESTRICTING_MAIN['id']}": RULESET_RESTRICTING_MAIN,
    }
    if detail_readable:
        fx[f"{base}/rulesets/{queue_ruleset['id']}"] = queue_ruleset
    return fx


def test_a_bypass_actor_on_an_active_queue_ruleset_fails() -> None:
    result = run_audit(
        REPO,
        _fixtures(_queue_ruleset(bypass_actors=[_ADMIN_ROLE_BYPASS])),
        branches="dev",
    )
    assert result.exit_code == 1, result.report
    assert "merge-queue ruleset 13269702 has bypass actor" in result.report
    assert "RepositoryRole:5:pull_request" in result.report


def test_an_unreadable_bypass_list_fails() -> None:
    result = run_audit(REPO, _fixtures(_unreadable_queue_ruleset()), branches="dev")
    assert result.exit_code == 1, result.report
    assert "bypass list of ruleset 13269702 is unreadable" in result.report


def test_an_unfetchable_ruleset_detail_fails() -> None:
    result = run_audit(
        REPO, _fixtures(_queue_ruleset(), detail_readable=False), branches="dev"
    )
    assert result.exit_code == 1, result.report
    assert "ruleset 13269702 is unreadable" in result.report


def test_an_empty_bypass_list_passes() -> None:
    result = run_audit(REPO, _fixtures(_queue_ruleset()), branches="dev")
    assert result.exit_code == 0, result.report
    assert "merge-queue ruleset 13269702 has no bypass actor" in result.report


def test_a_disabled_queue_ruleset_is_not_judged() -> None:
    disabled = _queue_ruleset(
        enforcement="disabled", bypass_actors=[_ADMIN_ROLE_BYPASS]
    )
    result = run_audit(REPO, _fixtures(disabled), branches="dev")
    assert result.exit_code == 0, result.report
    assert "no enforced merge-queue ruleset" in result.report
