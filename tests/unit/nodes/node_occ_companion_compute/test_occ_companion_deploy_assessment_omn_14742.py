# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary regression: the canonical OCC-companion producer must declare a
``dod-deploy-assessment`` dod_evidence item when the product PR touches
runtime/deploy-sensitive paths, so the product PR's required ``deploy-gate`` is
satisfied by the auto-authored OCC companion (F-05, OMN-14742).

The deploy-gate validator lives in omniclaude
(``.github/actions/deploy-gate/validate_pr_deploy_required.py``) and is NOT
importable from omnimarket at runtime or in CI (cross-repo boundary). Its
acceptance predicate — ``has_deploy_evidence``: a dod_evidence ``check_value``
contains one of ``docker exec`` / ``rpk topic produce`` / ``deploy`` — is small
and stable, so it is vendored below verbatim (same pattern as the
``lint-contract-check-values`` / substance-floor mirrors in
``test_occ_companion_substance_and_placeholder_omn_14679.py``). These tests drive
the REAL produced contract bytes through that predicate.

Proof structure (feedback_prove_red_against_exists_but_wrong):
  * GREEN — a runtime-touching PR's companion WITH the item → deploy evidence
    present, contract clears lint, occ-preflight stays eligible on both audiences.
  * RED control — the SAME companion with the deploy item stripped back out (the
    pre-F-05 shape) → deploy evidence ABSENT: the product PR's deploy-gate fails.
  * A non-runtime PR emits NO deploy item (no over-emission / dishonest receipt).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.validation.model_occ_eligibility_input import (
    ModelOccEligibilityInput,
)
from omnibase_core.validation.validator_occ_merge_eligibility import (
    validate_occ_merge_eligibility,
)

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    DEPLOY_ASSESSMENT_CHECK_VALUE,
    DEPLOY_ASSESSMENT_EVIDENCE_ID,
    find_deploy_sensitive_paths,
)

# --- verbatim from omniclaude/.github/actions/deploy-gate/validate_pr_deploy_required.py
# (OMN-8912). ``has_deploy_evidence`` NEVER executes the check — it substring-scans
# the check_value. Vendored because omniclaude is not importable in omnimarket CI;
# keep DEPLOY_KEYWORDS in sync with the canonical validator. -----------------------
_DEPLOY_KEYWORDS = ["docker exec", "rpk topic produce", "deploy"]


def _has_deploy_evidence(contract_path: Path) -> bool:
    """Faithful mirror of the deploy-gate ``has_deploy_evidence`` predicate."""
    data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    dod_evidence = data.get("dod_evidence", []) if isinstance(data, dict) else []
    for item in dod_evidence:
        checks = item.get("checks", []) if isinstance(item, dict) else []
        for check in checks:
            value = check.get("check_value", "") if isinstance(check, dict) else ""
            if isinstance(value, str) and any(
                kw in value.lower() for kw in _DEPLOY_KEYWORDS
            ):
                return True
    return False


# --- verbatim from onex_change_control/scripts/lint_contract_check_values.py --------
_GH_PR_PREFIX = ("gh pr checks", "gh pr view", "gh pr diff")
_HARDCODED_PR_NUMBER_RE = re.compile(r"gh pr (?:checks|view|diff)\s+\d+\s")
_BRACE_PR_RE = re.compile(r"\{pr\}")
_BRACE_REPO_RE = re.compile(r"\{repo\}")


def _lint_legacy_gh_pr(value: str) -> str | None:
    stripped = value.strip()
    if not stripped.startswith(_GH_PR_PREFIX):
        return None
    if _HARDCODED_PR_NUMBER_RE.search(stripped):
        return "hardcoded integer PR number"
    if _BRACE_PR_RE.search(stripped):
        return "wrong-format {pr} placeholder"
    if _BRACE_REPO_RE.search(stripped):
        return "wrong-format {repo} placeholder"
    if "${PR_NUMBER}" not in stripped:
        return "missing ${PR_NUMBER} placeholder"
    if "${REPO}" not in stripped and "--repo" not in stripped:
        return "missing --repo argument"
    return None


_TICKET = "OMN-14742"
_REPO = "OmniNode-ai/omnibase_infra"
_PR = 4321
_HEAD = "a1a2a3a4a5a6a7a8a9a0b1b2b3b4b5b6b7b8b9b0"
_OCC = 4400
_OCC_HEAD = "c1c2c3c4c5c6c7c8c9c0d1d2d3d4d5d6d7d8d9d0"
_RUNTIME_FILE = "src/omnibase_infra/nodes/node_x/handlers/handler_x.py"


def _probe(pr: int, state: str = "OPEN") -> ModelObservedProbe:
    return ModelObservedProbe(
        command=f"gh pr view {pr}",
        stdout=f'{{"number":{pr},"state":"{state}"}}',
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": _REPO,
        "pr_number": _PR,
        "pr_head_sha": _HEAD,
        "pr_title": f"fix({_TICKET}): runtime change",
        "pr_body": f"Closes {_TICKET}",
        "run_timestamp": "2026-07-17T00:00:00Z",
        "product_probe": _probe(_PR),
        "changed_files": (_RUNTIME_FILE,),
        "diff_total_lines": 12,
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


# Pass 2 add-on: the OCC companion PR is known, so the contract also declares the
# self-bind item — exercises the deploy item + self-bind item together.
_PASS2: dict[str, object] = {
    "occ_pr_number": _OCC,
    "occ_head_sha": _OCC_HEAD,
    "occ_probe": _probe(_OCC),
}


def _contract(plan: ModelOccCompanionPlan) -> str:
    return next(
        f.content
        for f in plan.companion_files
        if f.kind == EnumCompanionFileKind.CONTRACT
    )


def _dod_ids(contract_yaml: str) -> list[str]:
    return [i["id"] for i in yaml.safe_load(contract_yaml)["dod_evidence"]]


# ---------------------------------------------------------------------------
# Classifier pinning (drift-guard vs the canonical deploy-gate RUNTIME_PATH_PATTERNS)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeploySensitiveClassifier:
    @pytest.mark.parametrize(
        "path",
        [
            "src/omnibase_infra/nodes/node_x/handlers/handler_x.py",
            "src/omnibase_infra/nodes/node_x/contract.yaml",
            "src/omnibase_infra/runtime/service_kernel.py",
            "src/omnimarket/nodes/node_x/handlers/handler_x.py",
            "src/omnimarket/services/foo/bar.py",
            "src/omnibase_core/handlers/x.py",
            "src/omnibase_core/nodes/x.py",
            "src/foo/docker/compose.py",
            "src/foo/contract.yaml",
            "docker/Dockerfile.runtime",
            "Dockerfile",
            "scripts/monitor_logs.py",
        ],
    )
    def test_runtime_paths_hit(self, path: str) -> None:
        assert find_deploy_sensitive_paths((path,)) == (path,), path

    @pytest.mark.parametrize(
        "path",
        [
            "docs/readme.md",
            "README.md",
            "tests/unit/test_x.py",
            "src/omnibase_core/models/model_x.py",
            "src/foo/cli/cli_occ.py",  # CLI: intentionally NOT emitted (content-signal)
            "pyproject.toml",
        ],
    )
    def test_non_runtime_paths_miss(self, path: str) -> None:
        assert find_deploy_sensitive_paths((path,)) == (), path

    def test_returns_only_the_matching_subset(self) -> None:
        files = (_RUNTIME_FILE, "docs/readme.md", "Dockerfile")
        assert find_deploy_sensitive_paths(files) == (_RUNTIME_FILE, "Dockerfile")


# ---------------------------------------------------------------------------
# Cross-boundary: the produced contract must satisfy the deploy-gate predicate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeployAssessmentEmission:
    def test_runtime_pr_declares_deploy_item(self) -> None:
        contract = _contract(compute_companion_plan(_request()))
        assert DEPLOY_ASSESSMENT_EVIDENCE_ID in _dod_ids(contract)

    def test_runtime_pr_emits_backing_receipt_bound_to_product_pr(self) -> None:
        plan = compute_companion_plan(_request())
        receipts = [
            f
            for f in plan.companion_files
            if f.path.endswith(f"{DEPLOY_ASSESSMENT_EVIDENCE_ID}/command.yaml")
        ]
        assert len(receipts) == 1, "exactly one deploy-assessment receipt expected"
        parsed = yaml.safe_load(receipts[0].content)
        assert parsed["status"] == "PASS"
        assert parsed["pr_number"] == _PR  # bound to the PRODUCT PR
        assert parsed["evidence_item_id"] == DEPLOY_ASSESSMENT_EVIDENCE_ID
        # declared item -> receipt carries the per-entry hash the dual-accept gate wants
        assert parsed["contract_entry_sha256"].startswith("sha256:")
        assert receipts[0].contract_entry_sha256.startswith("sha256:")

    def test_green_deploy_gate_predicate_satisfied(self, tmp_path: Path) -> None:
        """GREEN: a runtime-touching PR companion carries deploy evidence."""
        contract = _contract(compute_companion_plan(_request()))
        cpath = tmp_path / "contracts" / f"{_TICKET}.yaml"
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(contract, encoding="utf-8")
        assert _has_deploy_evidence(cpath) is True

    def test_red_control_without_deploy_item_fails_deploy_gate(
        self, tmp_path: Path
    ) -> None:
        """RED control: strip the deploy-assessment item (pre-F-05 shape) and the
        product PR's deploy-gate predicate goes False — proving the item is what
        makes a runtime-touching companion clear the gate."""
        contract = _contract(compute_companion_plan(_request()))
        data = yaml.safe_load(contract)
        data["dod_evidence"] = [
            i for i in data["dod_evidence"] if i["id"] != DEPLOY_ASSESSMENT_EVIDENCE_ID
        ]
        assert DEPLOY_ASSESSMENT_EVIDENCE_ID not in [
            i["id"] for i in data["dod_evidence"]
        ]
        cpath = tmp_path / "contracts" / f"{_TICKET}.yaml"
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert _has_deploy_evidence(cpath) is False

    def test_non_runtime_pr_emits_no_deploy_item(self, tmp_path: Path) -> None:
        """No over-emission: a docs-only PR gets no deploy item (whose diff-scope
        assertion would be a false claim) and does not falsely clear the gate."""
        plan = compute_companion_plan(
            _request(changed_files=("docs/readme.md", "README.md"))
        )
        contract = _contract(plan)
        assert DEPLOY_ASSESSMENT_EVIDENCE_ID not in _dod_ids(contract)
        assert not [
            f
            for f in plan.companion_files
            if f.path.endswith(f"{DEPLOY_ASSESSMENT_EVIDENCE_ID}/command.yaml")
        ]
        cpath = tmp_path / "contracts" / f"{_TICKET}.yaml"
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(contract, encoding="utf-8")
        assert _has_deploy_evidence(cpath) is False


# ---------------------------------------------------------------------------
# The added item must not regress the other contract gates
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeployItemGateCompliance:
    def test_deploy_check_value_is_single_source(self) -> None:
        """The contract-declared deploy check equals the constant the receipt uses,
        so the declared check and the receipt check can never drift."""
        contract = _contract(compute_companion_plan(_request()))
        deploy_item = next(
            i
            for i in yaml.safe_load(contract)["dod_evidence"]
            if i["id"] == DEPLOY_ASSESSMENT_EVIDENCE_ID
        )
        assert len(deploy_item["checks"]) == 1
        assert deploy_item["checks"][0]["check_value"] == DEPLOY_ASSESSMENT_CHECK_VALUE

    def test_deploy_check_value_clears_placeholder_lint(self) -> None:
        assert _lint_legacy_gh_pr(DEPLOY_ASSESSMENT_CHECK_VALUE) is None

    def test_deploy_check_value_is_substantive_static_assert(self) -> None:
        # `| grep` static-assert derives L1 under the substance floor (OMN-14409);
        # not an existence probe (no bare `gh pr view --json <metadata>`).
        assert " | grep " in DEPLOY_ASSESSMENT_CHECK_VALUE
        assert "deploy" in DEPLOY_ASSESSMENT_CHECK_VALUE.lower()

    def test_deploy_description_line_stays_within_yamlfmt_wrap_width(self) -> None:
        """The deploy item's description line must stay <=100 chars so
        onex_change_control's yamlfmt (max_line_length: 100) does not FOLD it and
        restale contract_sha256 (F-03 / OMN-14684). Worst-case: longest product
        repo + 5-digit PR."""
        contract = _contract(
            compute_companion_plan(_request(repo=_REPO, pr_number=99999))
        )
        desc_lines = [
            ln
            for ln in contract.splitlines()
            if ln.lstrip().startswith("description:") and "Deploy-scope DoD" in ln
        ]
        assert desc_lines, "deploy description line not found"
        assert all(len(ln) <= 100 for ln in desc_lines), [
            (len(ln), ln) for ln in desc_lines
        ]


# ---------------------------------------------------------------------------
# Cross-boundary: adding the deploy item must not break occ-preflight
# ---------------------------------------------------------------------------


def _materialize(plan: ModelOccCompanionPlan, root: Path) -> None:
    for f in plan.companion_files:
        path = root / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.content, encoding="utf-8")


def _product_audience(root: Path) -> ModelOccEligibilityInput:
    return ModelOccEligibilityInput(
        repo=_REPO,
        pr_number=_PR,
        pr_title=f"fix({_TICKET}): runtime change",
        pr_body=(
            f"Closes {_TICKET}\nEvidence-Source: OCC#{_OCC}\nEvidence-Ticket: {_TICKET}"
        ),
        pr_branch=f"jonah/{_TICKET.lower()}",
        pr_commit_shas=(_HEAD,),
        pr_commit_texts=(f"fix({_TICKET})",),
        occ_commit_sha=_OCC_HEAD,
        contracts_dir=root / "contracts",
        receipts_dir=root / "drift" / "dod_receipts",
    )


def _occ_audience(root: Path) -> ModelOccEligibilityInput:
    return ModelOccEligibilityInput(
        repo="OmniNode-ai/onex_change_control",
        pr_number=_OCC,
        pr_title=f"evidence({_TICKET}): OCC companion",
        pr_body=f"Companion for {_TICKET}",
        pr_branch=f"auto/omninode-ai-omnibase-infra-pr-{_PR}-occ-autobind",
        pr_commit_shas=(_OCC_HEAD,),
        pr_commit_texts=(f"evidence({_TICKET})",),
        occ_commit_sha=_OCC_HEAD,
        contracts_dir=root / "contracts",
        receipts_dir=root / "drift" / "dod_receipts",
    )


@pytest.mark.unit
class TestDeployItemDoesNotBreakOccPreflight:
    def test_product_audience_still_eligible(self, tmp_path: Path) -> None:
        plan = compute_companion_plan(_request(**_PASS2))
        assert DEPLOY_ASSESSMENT_EVIDENCE_ID in _dod_ids(_contract(plan))
        _materialize(plan, tmp_path)
        result = validate_occ_merge_eligibility(_product_audience(tmp_path))
        assert result.eligible, result.detail

    def test_occ_companion_audience_still_eligible(self, tmp_path: Path) -> None:
        plan = compute_companion_plan(_request(**_PASS2))
        _materialize(plan, tmp_path)
        result = validate_occ_merge_eligibility(_occ_audience(tmp_path))
        assert result.eligible, result.detail
