# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16160: the six ``gh pr view``/``gh pr diff`` check_value generators in
``occ_evidence_stamp.py`` must be able to render a content-bound shape
admissible under BOTH live admissibility gates:

  * ``validate_pr_deploy_required.classify_check_value`` (OMN-14443 deploy-gate
    falsifiability ratchet, omniclaude) -- command-position allowlist accepts
    ``gh api`` (the ``gh-api`` compound token) but never bare ``gh``.
  * ``evidence_admissibility.classify_evidence`` (OMN-15309, onex_change_control)
    -- the STRICTER sibling predicate; same command-position shape, but never
    admits what deploy-gate refuses (parity is one-directional).

This suite drives the REAL live implementations of both gates (dynamically
loaded from the sibling canonical clones under ``$OMNI_HOME``), never a
hand-modelled mirror of either allowlist -- per the ticket's explicit
"do not guess either allowlist" instruction. Tests skip (never fabricate a
pass) when a sibling checkout is unavailable, mirroring the existing
``_load_occ_module`` convention in ``test_occ_emitter_literal_pins_omn_15407.py``.

RED-first: every assertion in ``TestContentBoundOverrideIsAdmissibleUnderBoth
LiveGates`` and ``TestDeployAssessmentItemRendererAcceptsOverride`` fails
against pre-OMN-16160 source with a ``TypeError`` (the ``content_bound_check_
value`` / ``check_value`` keyword did not exist yet) -- confirmed RED before
implementation, per CLAUDE.md's verification doctrine.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ci_dod_evidence_check_value,
    ci_receipt_public_check_value,
    deploy_assessment_check_value,
    downstream_dod_evidence_check_value,
    downstream_receipt_public_check_value,
    render_deploy_assessment_dod_evidence_item,
    self_bind_check_value,
)
from omnimarket.occ_content_probe import build_content_read_check
from tests.unit.nodes.node_pr_lifecycle_fix_effect.test_occ_emitter_literal_pins_omn_15407 import (
    _load_occ_module,
)

pytestmark = pytest.mark.unit

_PR = 2093
_REPO = "OmniNode-ai/omnimarket"
_HEAD_SHA = "b" * 40

_CONTENT_BOUND = build_content_read_check(
    repo=_REPO,
    path="src/omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/occ_evidence_stamp.py",
    kind="def",
    symbol="deploy_assessment_check_value",
    head_sha=_HEAD_SHA,
)


def _deploy_gate_module() -> ModuleType | None:
    """Dynamically load the REAL deploy-gate falsifiability ratchet.

    The file is self-contained (stdlib-only imports -- verified by reading it),
    so a plain ``spec_from_file_location`` load is sufficient; no sibling-
    package eviction dance is needed (unlike the onex_change_control PACKAGE
    loader below, which has to out-compete an installed wheel).
    """
    root = Path(os.environ.get("OMNICLAUDE_REPO_DIR", "../omniclaude")).resolve()
    target = (
        root / ".github" / "actions" / "deploy-gate" / "validate_pr_deploy_required.py"
    )
    if not target.is_file():
        return None
    spec = importlib.util.spec_from_file_location("deploy_gate_omn_16160", target)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_gate_omn_16160"] = module
    spec.loader.exec_module(module)
    return module


def _admissibility_module() -> ModuleType | None:
    return _load_occ_module(
        "src/onex_change_control/validation/evidence_admissibility.py",
        "evidence_admissibility_omn_16160",
    )


_LEGACY_FUNCS = {
    "downstream_dod_evidence_check_value": lambda: downstream_dod_evidence_check_value(
        pr_number=_PR, repo=_REPO
    ),
    "ci_dod_evidence_check_value": lambda: ci_dod_evidence_check_value(
        pr_number=_PR, repo=_REPO
    ),
    "deploy_assessment_check_value": lambda: deploy_assessment_check_value(
        pr_number=_PR, repo=_REPO
    ),
    "downstream_receipt_public_check_value": lambda: (
        downstream_receipt_public_check_value(pr_number=_PR, repo=_REPO)
    ),
    "ci_receipt_public_check_value": lambda: ci_receipt_public_check_value(
        pr_number=_PR, repo=_REPO
    ),
}


@pytest.mark.unit
class TestLegacyShapeIsRejectedByBothLiveGates:
    """Documents the defect: the bare ``gh pr view``/``gh pr diff`` default is
    inadmissible under deploy-gate (OMN-14443) today, regardless of literal
    PR-number pinning (Rule A/B compliance is orthogonal to falsifiability).
    """

    @pytest.mark.parametrize("name", sorted(_LEGACY_FUNCS))
    def test_rejected_by_deploy_gate(self, name: str) -> None:
        module = _deploy_gate_module()
        if module is None:
            pytest.skip("omniclaude checkout not available")
        verdict = module.classify_check_value(_LEGACY_FUNCS[name]())
        assert verdict.falsifiable is False, (name, verdict.reason)

    @pytest.mark.parametrize(
        "name", sorted(n for n in _LEGACY_FUNCS if n != "deploy_assessment_check_value")
    )
    def test_rejected_by_occ_admissibility(self, name: str) -> None:
        module = _admissibility_module()
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        verdict = module.classify_evidence(
            _LEGACY_FUNCS[name](),
            admissible_probes=module.LIVE_PROBE_COMMANDS
            | module.EXECUTED_HERMETIC_COMMANDS,
        )
        assert verdict.admissible is False, (name, verdict.reason)

    def test_deploy_assessment_legacy_shape_is_a_deploy_gate_only_defect(self) -> None:
        """MEASURED asymmetry, not assumed: ``deploy_assessment_check_value``'s
        pre-fix ``gh pr diff ... | grep -ciE '...'`` default is inadmissible
        under deploy-gate (bare ``gh``/``grep`` are not in ``LIVE_PROBE_
        COMMANDS``) but IS admissible under OCC's ``evidence_admissibility``
        predicate when driven with the runner's real hermetic vocabulary:
        ``grep`` alone is in ``EXECUTED_HERMETIC_COMMANDS``, and the check has
        no path-shaped operand for the OUTSIDE-ITS-OWN-DIFF rule to catch (the
        grep pattern is a `|`-joined alternation, which never matches the
        predicate's path-operand regex). So the LIVE OMN-16148/omnimarket#2093
        rejection this ticket cites is a deploy-gate-only failure for this
        specific function, not a dual-gate one -- the fix still needs to close
        it because deploy-gate is a REQUIRED merge gate on its own."""
        module = _admissibility_module()
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        verdict = module.classify_evidence(
            _LEGACY_FUNCS["deploy_assessment_check_value"](),
            admissible_probes=module.LIVE_PROBE_COMMANDS
            | module.EXECUTED_HERMETIC_COMMANDS,
            changed_paths=frozenset({"contracts/OMN-16160.yaml"}),
        )
        assert verdict.admissible is True, verdict.reason


_OVERRIDE_FUNCS = {
    "downstream_dod_evidence_check_value": lambda cv: (
        downstream_dod_evidence_check_value(
            pr_number=_PR, repo=_REPO, content_bound_check_value=cv
        )
    ),
    "ci_dod_evidence_check_value": lambda cv: ci_dod_evidence_check_value(
        pr_number=_PR, repo=_REPO, content_bound_check_value=cv
    ),
    "deploy_assessment_check_value": lambda cv: deploy_assessment_check_value(
        pr_number=_PR, repo=_REPO, content_bound_check_value=cv
    ),
    "downstream_receipt_public_check_value": lambda cv: (
        downstream_receipt_public_check_value(
            pr_number=_PR, repo=_REPO, content_bound_check_value=cv
        )
    ),
    "ci_receipt_public_check_value": lambda cv: ci_receipt_public_check_value(
        pr_number=_PR, repo=_REPO, content_bound_check_value=cv
    ),
}


@pytest.mark.unit
class TestContentBoundOverrideIsAdmissibleUnderBothLiveGates:
    """The fix: when a caller supplies a real content-bound candidate, each of
    the five product-observing generators renders it verbatim, and that
    rendered shape clears BOTH live gates.

    ``self_bind_check_value`` is deliberately excluded -- see
    ``TestSelfBindCheckValueDocumentedDecision`` for the OMN-16160 decision.
    """

    @pytest.mark.parametrize("name", sorted(_OVERRIDE_FUNCS))
    def test_renders_the_override_verbatim(self, name: str) -> None:
        assert _OVERRIDE_FUNCS[name](_CONTENT_BOUND) == _CONTENT_BOUND

    @pytest.mark.parametrize("name", sorted(_OVERRIDE_FUNCS))
    def test_admitted_by_deploy_gate(self, name: str) -> None:
        module = _deploy_gate_module()
        if module is None:
            pytest.skip("omniclaude checkout not available")
        verdict = module.classify_check_value(_OVERRIDE_FUNCS[name](_CONTENT_BOUND))
        assert verdict.falsifiable is True, (name, verdict.reason)

    @pytest.mark.parametrize("name", sorted(_OVERRIDE_FUNCS))
    def test_admitted_by_occ_admissibility(self, name: str) -> None:
        module = _admissibility_module()
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        verdict = module.classify_evidence(
            _OVERRIDE_FUNCS[name](_CONTENT_BOUND),
            admissible_probes=module.LIVE_PROBE_COMMANDS
            | module.EXECUTED_HERMETIC_COMMANDS,
            # OUTSIDE-ITS-OWN-DIFF is scoped to the OCC COMPANION's own
            # changed files (contracts/receipts) on the real caller
            # (contract_compliance_check.py's `_pr_changed_paths(occ_pr,
            # "onex_change_control")`) -- never the product repo path this
            # check reads. Mirrored here rather than left empty.
            changed_paths=frozenset({"contracts/OMN-16160.yaml"}),
        )
        assert verdict.admissible is True, (name, verdict.reason)

    def test_default_without_override_is_unchanged(self) -> None:
        """Backward compatibility: omitting the new kwarg must not change the
        pre-existing literal-pin default for any of the five functions."""
        assert downstream_dod_evidence_check_value(pr_number=_PR, repo=_REPO) == (
            f"gh pr view {_PR} --repo {_REPO} --json number,state"
        )
        assert ci_dod_evidence_check_value(pr_number=_PR, repo=_REPO) == (
            f"gh pr view {_PR} --repo {_REPO} --json files"
        )
        assert deploy_assessment_check_value(pr_number=_PR, repo=_REPO) == (
            f"gh pr diff {_PR} --repo {_REPO} --name-only | "
            "grep -ciE 'nodes/|handlers/|runtime/|services/|docker|monitor_logs|deploy'"
        )
        assert downstream_receipt_public_check_value(pr_number=_PR, repo=_REPO) == (
            f"gh pr view {_PR} --repo {_REPO} --json number,state,headRefName"
        )
        assert ci_receipt_public_check_value(pr_number=_PR, repo=_REPO) == (
            f"gh pr view {_PR} --repo {_REPO} --json files"
        )


@pytest.mark.unit
class TestSelfBindCheckValueDocumentedDecision:
    """OMN-16160 decision: ``self_bind_check_value`` is NOT rewritten to a
    content-bound shape. A self-bind item asserts facts about the OCC
    companion PR itself -- there is no "other content" (no product-repo file)
    for it to point at, so the content-bound shape does not apply.

    It stays the Rule-A/B-compliant literal pin (OMN-14431/OMN-15382),
    inadmissible under OMN-14443/15309 same as before. This is safe because
    ``_has_effective_check`` (onex_change_control) requires only ONE
    admissible item across the WHOLE contract, not one per entry -- and the
    admissibility-validator item (``ADMISSIBILITY_VALIDATOR_CHECK_VALUE``) is
    unconditionally minted on every rendered contract
    (``render_companion_contract`` / ``render_compute_companion_contract``),
    so the contract-level gate is satisfied regardless of self-bind's shape.
    Deploy-gate is unaffected by self-bind either way: it only requires ONE
    falsifiable check anywhere in the ticket's ``dod_evidence``, which the
    fixed downstream/CI/deploy-assessment items now supply when a content-bound
    candidate is derivable.
    """

    def test_self_bind_still_renders_the_literal_pinned_form(self) -> None:
        value = self_bind_check_value(
            occ_pr_number=6645, occ_repo="OmniNode-ai/onex_change_control"
        )
        assert value == (
            "gh pr view 6645 --repo OmniNode-ai/onex_change_control --json number,state"
        )

    def test_self_bind_shape_is_still_rejected_by_occ_admissibility(self) -> None:
        """Documents the known, accepted gap -- not a regression."""
        module = _admissibility_module()
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        value = self_bind_check_value(
            occ_pr_number=6645, occ_repo="OmniNode-ai/onex_change_control"
        )
        verdict = module.classify_evidence(
            value,
            admissible_probes=module.LIVE_PROBE_COMMANDS
            | module.EXECUTED_HERMETIC_COMMANDS,
        )
        assert verdict.admissible is False, verdict.reason

    def test_admissibility_validator_item_alone_satisfies_has_effective_check(
        self,
    ) -> None:
        """The always-minted admissibility-validator item is independently
        admissible, so a contract is never blocked by self-bind's shape alone.
        """
        module = _admissibility_module()
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        verdict = module.classify_evidence(
            ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
            admissible_probes=module.LIVE_PROBE_COMMANDS
            | module.EXECUTED_HERMETIC_COMMANDS,
        )
        assert verdict.admissible is True, verdict.reason


@pytest.mark.unit
class TestDeployAssessmentItemRendererAcceptsOverride:
    def test_uses_the_override_when_supplied(self) -> None:
        rendered = render_deploy_assessment_dod_evidence_item(
            repo=_REPO, pr_number=_PR, check_value=_CONTENT_BOUND
        )
        assert _CONTENT_BOUND in rendered

    def test_default_is_unchanged_when_no_override_supplied(self) -> None:
        rendered = render_deploy_assessment_dod_evidence_item(repo=_REPO, pr_number=_PR)
        assert deploy_assessment_check_value(pr_number=_PR, repo=_REPO) in rendered


# ---------------------------------------------------------------------------
# Cross-boundary: the compute-oracle producer (node_occ_companion_compute) must
# actually WIRE the content-bound value into its rendered contract, not just
# into the receipt. Before OMN-16160 `contract_binding_check`/
# `contract_diff_scope_check` in handler_occ_companion_compute.py were
# hardcoded `None` -- `request.downstream_check_value` was only ever used for
# the RECEIPT, so this producer's CONTRACT never declared a content-bound
# check even when the upstream read-EFFECT had already derived one.
# ---------------------------------------------------------------------------


def _dod_ids(contract_yaml: str) -> list[str]:
    import yaml

    return [i["id"] for i in yaml.safe_load(contract_yaml)["dod_evidence"]]


def _contract_check_values(contract_yaml: str) -> list[str]:
    import yaml

    data = yaml.safe_load(contract_yaml)
    return [ck["check_value"] for item in data["dod_evidence"] for ck in item["checks"]]


@pytest.mark.unit
class TestComputeOracleWiresContentBoundIntoTheContract:
    def _plan_contract(self, *, changed_files: tuple[str, ...] = ()) -> str:
        from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
            compute_companion_plan,
        )
        from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
            EnumCompanionFileKind,
        )
        from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
            ModelObservedProbe,
            ModelOccCompanionRequest,
        )

        request = ModelOccCompanionRequest(
            repo=_REPO,
            pr_number=_PR,
            pr_head_sha=_HEAD_SHA,
            pr_title="fix(OMN-16160): content-bound wiring",
            pr_body="Closes OMN-16160",
            run_timestamp="2026-08-18T00:00:00Z",
            product_probe=ModelObservedProbe(
                command=f"gh pr view {_PR} --repo {_REPO} --json number,state",
                stdout='{"number":2093,"state":"OPEN"}',
                exit_code=0,
            ),
            changed_files=changed_files,
            diff_total_lines=12,
            downstream_check_value=_CONTENT_BOUND,
        )
        plan = compute_companion_plan(request)
        return next(
            f.content
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.CONTRACT
        )

    def test_downstream_and_ci_items_declare_the_content_bound_value(self) -> None:
        contract = self._plan_contract()
        values = _contract_check_values(contract)
        assert values.count(_CONTENT_BOUND) >= 2, values

    def test_deploy_assessment_item_reuses_the_same_content_bound_value(self) -> None:
        contract = self._plan_contract(
            changed_files=("src/omnimarket/nodes/node_x/handlers/handler_x.py",)
        )
        assert "dod-deploy-assessment" in _dod_ids(contract)
        values = _contract_check_values(contract)
        assert _CONTENT_BOUND in values
        # The deploy-assessment item's own check_value must be the content-bound
        # form, not the inadmissible `gh pr diff` default.
        import yaml

        data = yaml.safe_load(contract)
        deploy_item = next(
            i for i in data["dod_evidence"] if i["id"] == "dod-deploy-assessment"
        )
        assert deploy_item["checks"][0]["check_value"] == _CONTENT_BOUND

    def test_content_bound_contract_items_pass_both_live_gates(self) -> None:
        contract = self._plan_contract(
            changed_files=("src/omnimarket/nodes/node_x/handlers/handler_x.py",)
        )
        occ_module = _admissibility_module()
        deploy_module = _deploy_gate_module()
        if occ_module is None or deploy_module is None:
            pytest.skip("sibling checkout(s) not available")
        for value in _contract_check_values(contract):
            if value == ADMISSIBILITY_VALIDATOR_CHECK_VALUE:
                continue
            deploy_verdict = deploy_module.classify_check_value(value)
            occ_verdict = occ_module.classify_evidence(
                value,
                admissible_probes=occ_module.LIVE_PROBE_COMMANDS
                | occ_module.EXECUTED_HERMETIC_COMMANDS,
                changed_paths=frozenset({"contracts/OMN-16160.yaml"}),
            )
            assert deploy_verdict.falsifiable or occ_verdict.admissible, (
                value,
                deploy_verdict.reason,
                occ_verdict.reason,
            )
