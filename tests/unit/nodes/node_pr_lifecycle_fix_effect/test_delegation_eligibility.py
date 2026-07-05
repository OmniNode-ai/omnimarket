# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for delegation_eligibility.is_delegation_eligible (OMN-13940)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.delegation_eligibility import (
    MAX_DELEGATION_FILES,
    MAX_DELEGATION_LINES,
    TWO_STRIKE_THRESHOLD,
    is_delegation_eligible,
)


def _eligible(**overrides: object) -> tuple[bool, str]:
    defaults: dict[str, object] = {
        "block_reason": "code_failure",
        "changed_files": ["src/foo.py"],
        "diff_total_lines": 5,
        "review_context_text": "",
        "strikes": 0,
    }
    defaults.update(overrides)
    return is_delegation_eligible(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
class TestIsDelegationEligible:
    def test_eligible_small_code_failure(self) -> None:
        eligible, reason = _eligible()
        assert eligible is True
        assert reason == "eligible"

    def test_eligible_changes_requested(self) -> None:
        eligible, _ = _eligible(block_reason="changes_requested")
        assert eligible is True

    @pytest.mark.parametrize(
        "reason",
        [
            "receipt_failure",
            "ci_failure",
            "conflict",
            "coderabbit",
            "deploy_gate_contract_not_found",
            "receipt_evidence_source_autobind",
        ],
    )
    def test_ineligible_block_reasons(self, reason: str) -> None:
        eligible, eligibility_reason = _eligible(block_reason=reason)
        assert eligible is False
        assert eligibility_reason.startswith("block_reason_not_eligible")

    def test_two_strike_threshold_trips(self) -> None:
        eligible, reason = _eligible(strikes=TWO_STRIKE_THRESHOLD)
        assert eligible is False
        assert reason == "two_strike_permanent_escalation"

    def test_below_two_strike_threshold_still_eligible(self) -> None:
        eligible, _ = _eligible(strikes=TWO_STRIKE_THRESHOLD - 1)
        assert eligible is True

    def test_empty_changed_files_ineligible(self) -> None:
        eligible, reason = _eligible(changed_files=[])
        assert eligible is False
        assert reason == "changed_files_unknown"

    def test_too_many_files_ineligible(self) -> None:
        files = [f"src/file_{i}.py" for i in range(MAX_DELEGATION_FILES + 1)]
        eligible, reason = _eligible(changed_files=files)
        assert eligible is False
        assert reason == "blast_radius_too_many_files"

    def test_at_files_limit_is_eligible(self) -> None:
        files = [f"src/file_{i}.py" for i in range(MAX_DELEGATION_FILES)]
        eligible, _ = _eligible(changed_files=files)
        assert eligible is True

    def test_too_many_lines_ineligible(self) -> None:
        eligible, reason = _eligible(diff_total_lines=MAX_DELEGATION_LINES + 1)
        assert eligible is False
        assert reason == "blast_radius_too_many_lines"

    @pytest.mark.parametrize(
        "path",
        [
            "onex_change_control/contracts/OMN-1.yaml",
            ".github/workflows/deploy-gate.yml",
            "scripts/no-raw-prod-bypass.sh",
            "onex_change_control/grants/prod_promotion_grants.yaml",
            "src/omnimarket/auth/handler.py",
        ],
    )
    def test_path_denylist_refuses(self, path: str) -> None:
        eligible, reason = _eligible(changed_files=[path])
        assert eligible is False
        assert reason.startswith("denylisted_path")

    @pytest.mark.parametrize(
        "text",
        [
            "this fixes a security vulnerability",
            "adds crypto key rotation",
            "SQL injection fix",
            "rotate the shared secret",
            "reset the user password flow",
            "refresh the auth token",
        ],
    )
    def test_content_keyword_denylist_refuses(self, text: str) -> None:
        eligible, reason = _eligible(review_context_text=text)
        assert eligible is False
        assert reason.startswith("denylisted_keyword")

    def test_content_denylist_case_insensitive(self) -> None:
        eligible, _ = _eligible(review_context_text="SECURITY hotfix")
        assert eligible is False
