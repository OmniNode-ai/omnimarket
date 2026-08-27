# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16788: a check the credential could not READ is not a check that FAILED.

Observed divergence (instrumented CI dispatch 33116161541 on ``dev``): every
single ``::pr-live-state`` check in the scheduled evidence sweep failed with

    could not resolve required status checks for OmniNode-ai/omnibase_infra@dev:
    classic=gh: Resource not accessible by integration (HTTP 403); rules=no names;
    could not positively confirm dev is absent — failing closed on unverifiable
    base-branch protection state (OMN-15715 D1)

while that same item's own declared sibling check PASSED. The runner's GitHub
App installation token can read PRs fine; what it cannot read is
``repos/{repo}/branches/{base}/protection/required_status_checks``, which needs
the ``administration: read`` scope. OMN-15715 D1 did exactly the right thing —
it refused to guess — but it recorded the refusal as a SUBSTANTIVE FAILURE,
which is indistinguishable in the receipt from "the evidence was read and found
wanting". That false-negative class is what gated ``--apply``.

The fix, and what these tests pin:

* an unreadable-protection outcome (HTTP 403 credential scope, or a bare HTTP
  404 meaning the repo is invisible to the credential) is recorded SKIPPED with
  a NAMED cause, never FAILED;
* it is emphatically NOT counted as verified either — a run carrying one
  blocks the ticket-level verdict from reaching VERIFIED, so the fail-closed
  intent of OMN-15715 D1 survives intact; the change is in HOW the block is
  recorded, never in WHETHER it blocks;
* every other cause stays exactly as it was: a confirmed "Branch not found"
  still takes the deleted-base carve-out, a confirmed "Branch not protected"
  still fails closed as a substantive failure, a timeout still fails closed,
  and a required context that genuinely ran RED still FAILS.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as hd_mod,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_REPO = "OmniNode-ai/omnibase_infra"
_PR = 2216
_SHA = "1ca9d929aa2aeddd48bbf23f5e2414a67e3bb9e9"
_MERGE_SHA = "9f3c1d2e4b5a60718293a4b5c6d7e8f90a1b2c3d"

# The two verbatim gh renderings this ticket is about, captured live
# 2026-08-27 (`gh api repos/<inaccessible>/branches/dev/protection/
# required_status_checks` -> "gh: Not Found (HTTP 404)"; the CI run's own
# log -> "gh: Resource not accessible by integration (HTTP 403)").
_STDERR_403 = "gh: Resource not accessible by integration (HTTP 403)"
_STDERR_404_REPO = "gh: Not Found (HTTP 404)"
_STDERR_404_BRANCH_ABSENT = "gh: Branch not found (HTTP 404)"
_STDERR_404_UNPROTECTED = "gh: Branch not protected (HTTP 404)"


# ---------------------------------------------------------------------------
# gh routing helper — patches the REAL subprocess boundary so the whole
# handler -> collector chain executes, rather than stubbing the very methods
# under test (feedback_real_dispatch_path_tests).
# ---------------------------------------------------------------------------


def _pr_view_json(*, state: str, merged: bool) -> str:
    return json.dumps(
        {
            "headRefName": "jonah/feature",
            "baseRefName": "dev",
            "headRefOid": _SHA,
            "state": state,
            "mergedAt": "2026-08-05T00:00:00Z" if merged else None,
            "mergeCommit": {"oid": _MERGE_SHA} if merged else None,
        }
    )


def _merge_state_json(*, state: str, merged: bool) -> str:
    """What ``gh pr view --json state,mergedAt`` returns (FETCH_PR_MERGE_STATE)."""
    return json.dumps(
        {"state": state, "mergedAt": "2026-08-05T00:00:00Z" if merged else None}
    )


def _lines(*objs: dict[str, object]) -> str:
    return "\n".join(json.dumps(o) for o in objs)


def _routed_gh(
    *,
    state: str = "MERGED",
    merged: bool = True,
    protection: str = "",
    protection_rc: int = 0,
    protection_stderr: str = "",
    protection_exc: Exception | None = None,
    rules: str = "[]",
    suites: str = "",
    runs: str = "",
    merge_runs: str = "",
) -> object:
    """Route every ``gh`` invocation the live-PR path makes to a fixture.

    Both ``gh pr view`` shapes are routed: the ``state,mergedAt`` projection
    (FETCH_PR_MERGE_STATE) and the wider
    ``headRefName,baseRefName,...`` projection (FETCH_PR_CHECKS_GREEN), keyed
    on the ``--json`` field list so one router serves the whole chain.
    """

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        argv = [str(a) for a in (list(args[0]) if args else [])]
        joined = " ".join(argv)
        if "view" in argv:
            # Keyed on ``headRefName``, not on ``state,mergedAt``: the WIDE
            # FETCH_PR_CHECKS_GREEN projection is
            # ``headRefName,baseRefName,headRefOid,state,mergedAt,mergeCommit``
            # and therefore CONTAINS the narrow projection as a substring.
            stdout = (
                _pr_view_json(state=state, merged=merged)
                if "headRefName" in joined
                else _merge_state_json(state=state, merged=merged)
            )
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=stdout, stderr=""
            )
        if "protection/required_status_checks" in joined:
            if protection_exc is not None:
                raise protection_exc
            return subprocess.CompletedProcess(
                args=argv,
                returncode=protection_rc,
                stdout=protection,
                stderr=protection_stderr,
            )
        if "rules/branches" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=rules, stderr=""
            )
        if "check-suites" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=suites, stderr=""
            )
        if "check-runs" in joined:
            body = merge_runs if f"commits/{_MERGE_SHA}/check-runs" in joined else runs
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=body, stderr=""
            )
        raise AssertionError(f"unrouted gh invocation: {argv}")

    return _run


def _checks_green_result(monkeypatch: pytest.MonkeyPatch, **routing: object) -> object:
    monkeypatch.setattr(hd_mod.subprocess, "run", _routed_gh(**routing))  # type: ignore[arg-type]
    command = ModelDodEvidenceGithubLookupCommand(
        operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
        repo=_REPO,
        pr_number=_PR,
    )
    return HandlerDodEvidenceGithubEffect().handle(command).events[0]


# ---------------------------------------------------------------------------
# 1. The EFFECT handler classifies WHY the protection probe produced no names.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProtectionProbeUnreachableClassification:
    """403 vs bare-404 vs the two confirmed-branch 404s vs transport failure."""

    def test_403_on_protection_probe_is_classified_credential_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact CI signature. ``checks_green`` stays False (fail-closed,
        unchanged) but the result now carries the NAMED cause the caller needs
        to record SKIPPED instead of FAILED, and the detail names the missing
        scope so an operator reading the receipt knows what to grant."""
        result = _checks_green_result(
            monkeypatch,
            protection_rc=1,
            protection_stderr=_STDERR_403,
        )
        assert result.checks_green is False
        assert (
            result.unreachable_cause
            == EnumEvidenceUnverifiableCause.CREDENTIAL_CANNOT_READ_BRANCH_PROTECTION
        )
        assert "administration: read" in (result.detail or "")

    def test_bare_404_on_protection_probe_is_classified_repo_not_accessible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo absent from the App installation returns a BARE ``Not Found
        (HTTP 404)`` on this endpoint — verified live 2026-08-27. Same SKIPPED
        treatment, DIFFERENT named cause and reason string: the remedy is
        adding the repo to the installation, not widening a scope."""
        result = _checks_green_result(
            monkeypatch,
            protection_rc=1,
            protection_stderr=_STDERR_404_REPO,
        )
        assert result.checks_green is False
        assert (
            result.unreachable_cause
            == EnumEvidenceUnverifiableCause.REPO_NOT_ACCESSIBLE_TO_CREDENTIAL
        )
        detail = (result.detail or "").lower()
        assert "installation" in detail
        assert "administration: read" not in detail

    def test_confirmed_unprotected_base_is_not_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15715 D2 must survive untouched: a base branch CONFIRMED alive
        and simply never protected is a substantive finding (the gaming
        vector), NOT an unreadable one. It stays FAILED with no cause."""
        result = _checks_green_result(
            monkeypatch,
            protection_rc=1,
            protection_stderr=_STDERR_404_UNPROTECTED,
        )
        assert result.checks_green is False
        assert result.unreachable_cause is None
        assert "no branch protection governed" in (result.detail or "")

    def test_confirmed_deleted_base_carveout_is_not_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15715 D1's positive carve-out must survive untouched: a
        CONFIRMED-absent base still resolves GREEN off own-branch history and
        carries no unreachable cause."""
        result = _checks_green_result(
            monkeypatch,
            protection_rc=1,
            protection_stderr=_STDERR_404_BRANCH_ABSENT,
            suites=_lines({"id": 1, "head_branch": "jonah/feature"}),
            runs=_lines(
                {
                    "name": "ci / build",
                    "status": "completed",
                    "conclusion": "success",
                    "check_suite": {"id": 1},
                    "id": 11,
                    "completed_at": "2026-08-05T00:00:00Z",
                }
            ),
        )
        assert result.checks_green is True
        assert result.unreachable_cause is None

    def test_transport_failure_on_protection_probe_is_not_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timeout is NOT a credential fact. Widening the SKIPPED class to
        every unresolvable probe would let a flaky network launder an unread
        check into a non-failure; only the two POSITIVELY-identified
        credential renderings qualify. Timeout stays FAILED, cause None."""
        result = _checks_green_result(
            monkeypatch,
            protection_exc=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        )
        assert result.checks_green is False
        assert result.unreachable_cause is None

    def test_genuinely_red_required_check_is_not_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The load-bearing negative control: protection READS FINE and a
        required context is RED. That is a substantive failure and must keep
        failing — this ticket must not launder red CI into 'unverifiable'."""
        result = _checks_green_result(
            monkeypatch,
            state="OPEN",
            merged=False,
            protection=json.dumps({"contexts": ["CI Summary"], "checks": []}),
            suites=_lines({"id": 1, "head_branch": "jonah/feature"}),
            runs=_lines(
                {
                    "name": "CI Summary",
                    "status": "completed",
                    "conclusion": "failure",
                    "check_suite": {"id": 1},
                    "id": 11,
                    "completed_at": "2026-08-05T00:00:00Z",
                }
            ),
        )
        assert result.checks_green is False
        assert result.unreachable_cause is None

    def test_open_pr_with_403_protection_is_classified_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The not-yet-merged early return is a SECOND fail-closed exit from
        the same unresolvable probe. It must classify identically, or an OPEN
        PR's unreadable protection would still be recorded as substantive."""
        result = _checks_green_result(
            monkeypatch,
            state="OPEN",
            merged=False,
            protection_rc=1,
            protection_stderr=_STDERR_403,
        )
        assert result.checks_green is False
        assert (
            result.unreachable_cause
            == EnumEvidenceUnverifiableCause.CREDENTIAL_CANNOT_READ_BRANCH_PROTECTION
        )


# ---------------------------------------------------------------------------
# 2. The collector records it SKIPPED-with-cause, not FAILED.
# ---------------------------------------------------------------------------


def _write_contract(occ_root: Path, ticket_id: str, items: list[dict]) -> Path:
    contracts_dir = occ_root / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    path = contracts_dir / f"{ticket_id}.yaml"
    path.write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": ticket_id,
                "dod_evidence": items,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_receipt(occ_root: Path, ticket_id: str, item_id: str) -> None:
    receipt_dir = occ_root / "drift" / "dod_receipts" / ticket_id / item_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / "command.yaml").write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": ticket_id,
                "evidence_item_id": item_id,
                "check_type": "command",
                "check_value": (
                    f"grep -q '^status: PASS$' "
                    f"drift/dod_receipts/{ticket_id}/{item_id}/command.yaml"
                ),
                "status": "PASS",
                "commit_sha": _SHA,
                "run_timestamp": "2026-08-27T21:04:00Z",
                "runner": "claude",
                "verifier": "claude-review",
                "probe_command": (
                    f"gh pr view {_PR} --repo {_REPO} --json number,state"
                ),
                "pr_number": _PR,
            }
        ),
        encoding="utf-8",
    )


_ITEM_ID = f"dod-omnibase_infra-pr-{_PR}"


def _pr_bound_item(ticket_id: str) -> dict:
    return {
        "id": _ITEM_ID,
        "description": f"omnibase_infra PR #{_PR} carries the change",
        "checks": [
            {
                "check_type": "command",
                "check_value": (
                    f"grep -q '^status: PASS$' "
                    f"drift/dod_receipts/{ticket_id}/{_ITEM_ID}/command.yaml"
                ),
            }
        ],
    }


@pytest.fixture
def occ_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    occ_root = tmp_path / "onex_change_control"
    occ_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.delenv("DOD_VERIFY_LIVE_PR_CHECK", raising=False)
    return occ_root


def _collect_live(occ_env: Path, ticket: str) -> ModelEvidenceCheckResult:
    contract = _write_contract(occ_env, ticket, [_pr_bound_item(ticket)])
    _write_receipt(occ_env, ticket, _ITEM_ID)
    results = EvidenceCollector().collect(ticket, contract_path=str(contract))
    live = [r for r in results if "live-state" in r.evidence_id]
    assert len(live) == 1, [r.evidence_id for r in results]
    return live[0]


@pytest.mark.unit
class TestCollectorRecordsUnreadableAsSkipped:
    def test_merged_pr_with_unreadable_protection_is_skipped_not_failed(
        self, occ_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The headline case: PR is MERGED, the ONLY thing standing between
        this check and a verdict is a protection read the credential cannot
        make. SKIPPED with a named cause — the failure count must stay clean
        so the gap arithmetic reports a shortfall, not a defect."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(protection_rc=1, protection_stderr=_STDERR_403),
        )
        live = _collect_live(occ_env, "OMN-16788T")
        assert live.status == EnumEvidenceCheckStatus.SKIPPED
        assert (
            live.unverifiable_cause
            == EnumEvidenceUnverifiableCause.CREDENTIAL_CANNOT_READ_BRANCH_PROTECTION
        )
        assert "administration: read" in (live.message or "")

    def test_repo_invisible_to_credential_is_skipped_with_its_own_reason(
        self, occ_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(protection_rc=1, protection_stderr=_STDERR_404_REPO),
        )
        live = _collect_live(occ_env, "OMN-16788T")
        assert live.status == EnumEvidenceCheckStatus.SKIPPED
        assert (
            live.unverifiable_cause
            == EnumEvidenceUnverifiableCause.REPO_NOT_ACCESSIBLE_TO_CREDENTIAL
        )
        assert "installation" in (live.message or "").lower()

    def test_unmerged_pr_with_unreadable_protection_still_fails(
        self, occ_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anti-laundering control. An OPEN PR is a SUBSTANTIVE finding read
        straight off a reachable API — the credential gap does not excuse it.
        A degrade that swallowed this would turn every un-merged PR in an
        unreadable-protection repo into a non-failure."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                state="OPEN",
                merged=False,
                protection_rc=1,
                protection_stderr=_STDERR_403,
            ),
        )
        live = _collect_live(occ_env, "OMN-16788T")
        assert live.status == EnumEvidenceCheckStatus.FAILED
        assert live.unverifiable_cause is None
        assert "not merged" in (live.message or "").lower()

    def test_merged_pr_with_red_required_check_still_fails(
        self, occ_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anti-laundering control 2: protection reads fine, a required
        context is RED. Unchanged FAILED."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                protection=json.dumps({"contexts": ["CI Summary"], "checks": []}),
                suites=_lines({"id": 1, "head_branch": "jonah/feature"}),
                runs=_lines(
                    {
                        "name": "CI Summary",
                        "status": "completed",
                        "conclusion": "failure",
                        "check_suite": {"id": 1},
                        "id": 11,
                        "completed_at": "2026-08-05T00:00:00Z",
                    }
                ),
                merge_runs="",
            ),
        )
        live = _collect_live(occ_env, "OMN-16788T")
        assert live.status == EnumEvidenceCheckStatus.FAILED
        assert live.unverifiable_cause is None


# ---------------------------------------------------------------------------
# 3. SKIPPED-with-cause blocks the flip — it is NOT counted as verified.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnverifiableBlocksTheVerdict:
    """The half that preserves OMN-15715's fail-closed intent.

    Recording the unreadable check as SKIPPED removes it from the FAILURE
    count. On its own that would be a fail-OPEN regression, because
    ``HandlerDodVerify`` reaches VERIFIED whenever ``failed == 0`` and at
    least one check verified — an ordinary SKIPPED (e.g. OMN-16087's
    intentional non-merged assertion) is deliberately non-blocking. An
    UNVERIFIABLE skip is a different animal and must block.
    """

    @staticmethod
    def _verdict(checks: list[ModelEvidenceCheckResult]) -> ModelDodVerifyState:
        command = ModelDodVerifyStartCommand(
            correlation_id=uuid4(), ticket_id="OMN-16788T", dry_run=True
        )
        state = HandlerDodVerify().handle(command, evidence_results=checks)
        assert isinstance(state, ModelDodVerifyState)
        return state

    def test_one_unverifiable_skip_prevents_verified_even_with_a_green_sibling(
        self,
    ) -> None:
        checks = [
            ModelEvidenceCheckResult(
                evidence_id=_ITEM_ID,
                description="declared check",
                status=EnumEvidenceCheckStatus.VERIFIED,
                message="ok",
            ),
            ModelEvidenceCheckResult(
                evidence_id=f"{_ITEM_ID}::pr-live-state",
                description="live PR state",
                status=EnumEvidenceCheckStatus.SKIPPED,
                message="credential cannot read branch protection",
                unverifiable_cause=(
                    EnumEvidenceUnverifiableCause.CREDENTIAL_CANNOT_READ_BRANCH_PROTECTION
                ),
            ),
        ]
        state = self._verdict(checks)
        assert state.status == EnumDodVerifyStatus.SKIPPED
        assert state.failed_count == 0
        assert state.verified_count == 1
        assert "EVIDENCE_UNVERIFIABLE" in (state.error_message or "")

    def test_an_ordinary_skip_still_does_not_block(self) -> None:
        """Scope control: the existing non-blocking SKIPPED semantics (a
        deliberate OMN-16087 assertion skip, a disabled live check) are
        untouched — only a skip carrying an unverifiable CAUSE blocks."""
        checks = [
            ModelEvidenceCheckResult(
                evidence_id=_ITEM_ID,
                description="declared check",
                status=EnumEvidenceCheckStatus.VERIFIED,
                message="ok",
            ),
            ModelEvidenceCheckResult(
                evidence_id=f"{_ITEM_ID}::pr-live-state",
                description="live PR state",
                status=EnumEvidenceCheckStatus.SKIPPED,
                message="intentional non-merged assertion (OMN-16087)",
            ),
        ]
        state = self._verdict(checks)
        assert state.status == EnumDodVerifyStatus.VERIFIED

    def test_a_real_failure_still_dominates_an_unverifiable_skip(self) -> None:
        """Precedence: FAILED wins. An unreadable check must never downgrade
        a run that also contains a substantive red into a mere SKIPPED."""
        checks = [
            ModelEvidenceCheckResult(
                evidence_id="dod-other",
                description="declared check",
                status=EnumEvidenceCheckStatus.FAILED,
                message="receipt missing",
            ),
            ModelEvidenceCheckResult(
                evidence_id=f"{_ITEM_ID}::pr-live-state",
                description="live PR state",
                status=EnumEvidenceCheckStatus.SKIPPED,
                message="credential cannot read branch protection",
                unverifiable_cause=(
                    EnumEvidenceUnverifiableCause.CREDENTIAL_CANNOT_READ_BRANCH_PROTECTION
                ),
            ),
        ]
        state = self._verdict(checks)
        assert state.status == EnumDodVerifyStatus.FAILED
        assert state.failed_count == 1


@pytest.mark.unit
class TestUnverifiableCauseIsStructurallyConstrained:
    def test_cause_is_rejected_on_every_status_except_skipped(self) -> None:
        """A cause means 'this check did not run'. Allowing it on any status
        that says it DID run would let the field contradict the status it is
        supposed to explain. Enumerated exhaustively over the enum rather than
        over a hand-listed subset, so a future status member is covered the
        day it is added instead of silently escaping the invariant."""
        for status in EnumEvidenceCheckStatus:
            if status is EnumEvidenceCheckStatus.SKIPPED:
                continue
            with pytest.raises(ValueError, match="only valid on a SKIPPED result"):
                ModelEvidenceCheckResult(
                    evidence_id="dod-x",
                    description="x",
                    status=status,
                    unverifiable_cause=(
                        EnumEvidenceUnverifiableCause.CREDENTIAL_CANNOT_READ_BRANCH_PROTECTION
                    ),
                )
