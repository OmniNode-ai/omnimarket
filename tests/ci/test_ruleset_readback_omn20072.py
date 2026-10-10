# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20072 AC3, ruleset half: the repo-owned gate is required and no OCC context is.

At S6 the repo-owned evidence gate became the required context on omnimarket
``dev`` and the four OCC contexts were removed from branch protection. That is a
fact about GitHub, not about a file in this repository, so these tests read it:
the active rules for ``dev`` and the ``dev`` branch protection, through the same
``gh`` reader the verifier uses for required contexts. They only read; nothing
here writes protection. Every read is public data on a public repository, and a
read that fails fails the test; it never skips. The pure evaluators are also fed
planted payloads that must violate them.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    _gh_json,
    _required_check_names_from_classic,
    _required_check_names_from_rules,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
BRANCH = "dev"
EVIDENCE_CONTEXT = "repo-evidence / dod-verify"
GITHUB_ACTIONS_APP_ID = 15368
GH_TIMEOUT_S = 60

# The four contexts S6 removed from dev branch protection
# (rolling ledger STATUS 2026-10-07T10:06:12Z, the BEFORE list).
OCC_CONTEXTS = frozenset(
    {
        "OCC Emitter Golden Gate",
        "ONEX Change Control Schema Compatibility",
        "call-reject-skip-token / occ-preflight / eligibility",
        "occ-preflight / eligibility",
    }
)


def _read(path: str) -> Any:
    data, detail = _gh_json(["gh", "api", path], GH_TIMEOUT_S)
    if data is None:
        pytest.fail(f"GitHub read of {path} failed: {detail}")
    return data


def _dev_rules() -> list[dict[str, Any]]:
    rules = _read(f"repos/{REPO}/rules/branches/{BRANCH}")
    assert isinstance(rules, list)
    return rules


def _dev_classic_required_contexts() -> set[str]:
    branch = _read(f"repos/{REPO}/branches/{BRANCH}")
    assert isinstance(branch, dict)
    protection = branch.get("protection")
    assert isinstance(protection, dict), "dev carries no branch protection"
    return _required_check_names_from_classic(protection.get("required_status_checks"))


def evidence_source_violations(rules: list[dict[str, Any]]) -> list[str]:
    """The ruleset requires the repo-evidence context from GitHub Actions only."""
    entries = [
        check
        for rule in rules
        if rule.get("type") == "required_status_checks"
        for check in rule.get("parameters", {}).get("required_status_checks", [])
        if check.get("context") == EVIDENCE_CONTEXT
    ]
    if not entries:
        return [f"no active rule on {BRANCH} requires {EVIDENCE_CONTEXT!r}"]
    return [
        f"{EVIDENCE_CONTEXT!r} is required with integration_id "
        f"{entry.get('integration_id')!r}, not GitHub Actions "
        f"({GITHUB_ACTIONS_APP_ID})"
        for entry in entries
        if entry.get("integration_id") != GITHUB_ACTIONS_APP_ID
    ]


def occ_context_violations(contexts: set[str]) -> list[str]:
    """Any OCC context still required."""
    return [
        f"{name!r} is still required"
        for name in sorted(contexts)
        if name in OCC_CONTEXTS or "occ-preflight" in name.lower()
    ]


def test_s6_ruleset_requires_repo_evidence_and_no_occ_context() -> None:
    rules = _dev_rules()
    assert evidence_source_violations(rules) == []
    contexts = _dev_classic_required_contexts() | _required_check_names_from_rules(
        rules
    )
    assert EVIDENCE_CONTEXT in contexts
    assert occ_context_violations(contexts) == []


def test_s6_ruleset_planted_payloads_violate() -> None:
    no_source = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": EVIDENCE_CONTEXT}]},
        }
    ]
    assert evidence_source_violations(no_source)
    assert evidence_source_violations([])
    wrong_app = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": EVIDENCE_CONTEXT, "integration_id": 1}
                ],
            },
        }
    ]
    assert evidence_source_violations(wrong_app)
    assert occ_context_violations({"CI Summary", "occ-preflight / eligibility"})
    assert occ_context_violations({"CI Summary"}) == []
