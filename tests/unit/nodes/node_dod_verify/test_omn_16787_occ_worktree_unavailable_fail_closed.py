# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16787 — the OTHER half of OMN-15454's fail-closed rule.

``_materialize_occ_dev_worktree`` has two independent failure classes, and
OMN-15454 only closed one of them:

* ``git fetch`` failed -> ``FETCH_FAILED`` -> ``collect()`` refuses. Closed.
* ``git worktree add`` failed or timed out -> ``(None, None, ...)`` ->
  ``collect()`` silently fell through to ``_DEFAULT_CONTRACT_ROOTS``, i.e. the
  ``main``-tracking working tree, **while the run still stamped**
  ``occ_governance_ref: origin/dev``. Open.

The second path is a false-negative generator on the only sanctioned Done-flip
tool: OCC ``dev`` runs thousands of commits ahead of ``main``, so a dev-only
contract is invisible to the working tree and the run reports
``CONTRACT_MISSING`` — "the clone could not be read at the ref I claim I read"
laundered into "no contract exists". Measured 2026-08-27: 3 of 22
``CONTRACT_MISSING`` results in the beta sweep were this artifact, with the
canonical clone 33 commits behind ``origin/dev``.

The trigger is ordinary load, not a rare fault: a real ``git worktree add`` of
the OCC repo checks out 32,382 files in ~34.5 s single-threaded, against a
60 s ceiling, under a 5-way-parallel sweep.

RED-first against the ticket's acceptance criteria, driving the real
``EvidenceCollector.collect()`` path with real git subprocesses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    EnumOccRefRefreshOutcome,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _ALLOW_STALE_OCC_REF_ENV,
    _DEFAULT_GIT_OP_TIMEOUT_S,
    _GIT_OP_TIMEOUT_ENV,
    EvidenceCollector,
)

_TICKET = "OMN-16787-fixture"

# The measured cold checkout of the live OCC repo (32,382 files) that made the
# old 60 s ceiling a routine trip rather than a fault signal.
_MEASURED_COLD_OCC_CHECKOUT_S = 34.5


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_contract(occ: Path, ticket: str, item_id: str) -> None:
    contract_dir = occ / "contracts"
    contract_dir.mkdir(exist_ok=True)
    (contract_dir / f"{ticket}.yaml").write_text(
        "schema_version: '1.0.0'\n"
        f"ticket_id: {ticket}\n"
        "dod_evidence:\n"
        f"  - id: {item_id}\n"
        "    description: trivially true\n"
        "    checks:\n"
        "      - check_type: command\n"
        "        check_value: 'true'\n",
        encoding="utf-8",
    )


def _occ_with_worktree_content_only(tmp_path: Path) -> Path:
    """An OCC clone whose WORKING TREE carries the contract but whose
    ``origin/dev`` ref does not exist.

    This is the production shape in miniature: the contract is reachable via
    ``_DEFAULT_CONTRACT_ROOTS`` (the stale ``main``-tracking checkout) and NOT
    via the governance ref. If the collector silently falls back, it "finds"
    the contract and attributes the verdict to ``origin/dev``; if it fails
    closed, it says it could not read the ref.

    ``git worktree add --detach --force <tmp> origin/dev`` against an absent
    ref is a real, deterministic ``git worktree add`` failure — the same
    ``(None, None, ...)`` return the production timeout produces, reached
    through the real code path rather than by mocking the method under test.
    """
    occ = tmp_path / "onex_change_control"
    occ.mkdir()
    _git(occ, "init", "-q")
    _git(occ, "config", "user.email", "t@t.co")
    _git(occ, "config", "user.name", "t")
    _git(occ, "checkout", "-q", "-b", "main")
    _write_contract(occ, _TICKET, "dod-working-tree-only")
    _git(occ, "add", "-A")
    _git(occ, "commit", "-q", "-m", "main contract")
    return occ


def _occ_with_real_dev_ref(tmp_path: Path) -> Path:
    """An OCC clone with a genuine ``origin/dev`` remote-tracking ref."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t.co")
    _git(seed, "config", "user.name", "t")
    _git(seed, "checkout", "-q", "-b", "dev")
    _write_contract(seed, _TICKET, "dod-on-dev")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed dev")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "dev")

    occ = tmp_path / "onex_change_control"
    _git(tmp_path, "clone", "-q", str(remote), str(occ))
    _git(occ, "checkout", "-q", "-b", "main")
    return occ


def _stub_fetch_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the refresh leg to succeed so the test isolates the worktree leg.

    Without this a fetch failure would short-circuit into the OMN-15454
    refusal and the worktree branch would never be reached — the test would
    pass for the wrong reason.
    """

    def _fake_run_occ_fetch(
        self: EvidenceCollector, occ_path: Path, remote: str, branch: str
    ) -> tuple[EnumOccRefRefreshOutcome, str]:
        return EnumOccRefRefreshOutcome.FETCHED, ""

    monkeypatch.setattr(EvidenceCollector, "_run_occ_fetch", _fake_run_occ_fetch)


@pytest.mark.unit
def test_worktree_materialisation_failure_refuses_by_default_ac1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 — the worktree could not be materialised, so no verdict is reached.

    The decisive assertion is the NEGATIVE one: the contract sitting in the
    working tree must NOT be picked up. A silent fallback here is exactly the
    defect — it produces either a fabricated verdict or, when the working tree
    is behind, a ``CONTRACT_MISSING`` that reads as "this ticket has no
    contract".
    """
    occ = _occ_with_worktree_content_only(tmp_path)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv(_ALLOW_STALE_OCC_REF_ENV, raising=False)
    _stub_fetch_ok(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    assert len(results) == 1
    assert results[0].evidence_id == "occ_worktree_unavailable"
    # OMN-17796 re-encoded this refusal: the check is SKIPPED (nothing ran, so
    # nothing failed) and the RUN is UNRESOLVED with a typed cause, because
    # FAILED asserted a red about a ticket whose contract had not been loaded.
    # The refusal itself — this test's subject — is unchanged.
    assert results[0].status == EnumEvidenceCheckStatus.SKIPPED
    assert results[0].status != EnumEvidenceCheckStatus.VERIFIED
    assert collector.occ_ref_failure_cause is (
        EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE
    )
    assert "OCC_WORKTREE_UNAVAILABLE" in (results[0].message or "")
    # Names the ref it could not materialise, and the override that would
    # proceed anyway — an operator must not have to read the source to act.
    assert "origin/dev" in (results[0].message or "")
    assert _ALLOW_STALE_OCC_REF_ENV in (results[0].message or "")

    # The working-tree contract was NOT silently substituted.
    assert not any(r.evidence_id == "dod-working-tree-only" for r in results)
    # And the refusal is not disguised as "no contract exists".
    assert not any(
        r.evidence_id == "contract" and r.status == EnumEvidenceCheckStatus.SKIPPED
        for r in results
    )


@pytest.mark.unit
def test_worktree_failure_override_marks_every_result_unattributable_ac2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 — the override proceeds but discloses, exactly as FETCH_FAILED does.

    Same contract as OMN-15454's escape hatch: proceeding is allowed, but
    every result it produces is marked un-attributable to a verified-fresh
    governance ref, plus a standalone item names the override.
    """
    occ = _occ_with_worktree_content_only(tmp_path)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv(_ALLOW_STALE_OCC_REF_ENV, "1")
    _stub_fetch_ok(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    real_check = next(r for r in results if r.evidence_id == "dod-working-tree-only")
    assert real_check.status == EnumEvidenceCheckStatus.VERIFIED
    assert "UNATTRIBUTABLE" in (real_check.message or "")

    override_item = next(
        r for r in results if r.evidence_id == "occ_worktree_unavailable_override"
    )
    assert override_item.status == EnumEvidenceCheckStatus.SKIPPED
    assert _ALLOW_STALE_OCC_REF_ENV in (override_item.message or "")


@pytest.mark.unit
def test_no_occ_root_still_reports_contract_missing_not_a_worktree_refusal_ac3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3 — non-regression: "there is no OCC repo here" is not this defect.

    When no OCC root resolves at all, no fetch is attempted and
    ``refresh_outcome`` is ``None``. That is a legitimate pre-existing shape
    and must keep its existing ``CONTRACT_MISSING`` reporting rather than
    being relabelled as a worktree fault — otherwise the new refusal would
    fire on environments that never had an OCC clone to begin with.
    """
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))  # no onex_change_control under it
    monkeypatch.delenv(_ALLOW_STALE_OCC_REF_ENV, raising=False)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    assert collector.occ_refresh_outcome is None
    assert not any(
        r.evidence_id.startswith("occ_worktree_unavailable") for r in results
    )
    assert len(results) == 1
    assert results[0].evidence_id == "contract"
    assert results[0].status == EnumEvidenceCheckStatus.SKIPPED


@pytest.mark.unit
def test_git_op_timeout_default_exceeds_the_measured_cold_occ_checkout_ac4() -> None:
    """AC4 — the ceiling is above a real cold OCC checkout, with headroom.

    A 60 s ceiling against a measured 34.5 s checkout leaves no margin for the
    5-way-parallel contention the sweep actually runs under, which is why the
    fail-closed path below would otherwise fire on ordinary load instead of on
    a genuine fault.
    """
    assert _DEFAULT_GIT_OP_TIMEOUT_S > _MEASURED_COLD_OCC_CHECKOUT_S * 2


@pytest.mark.unit
def test_git_op_timeout_is_operator_configurable_and_times_out_closed_ac4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4 — the env override is honoured, and a timed-out add fails closed.

    Drives the real ``subprocess.TimeoutExpired`` branch of the real
    ``git worktree add`` (rather than the invalid-ref branch the other tests
    use), proving both failure modes converge on the same refusal. The OCC
    fixture here has a genuine ``origin/dev`` carrying the contract, so a
    silent fallback would have produced a fully VERIFIED table.
    """
    occ = _occ_with_real_dev_ref(tmp_path)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv(_ALLOW_STALE_OCC_REF_ENV, raising=False)
    monkeypatch.setenv(_GIT_OP_TIMEOUT_ENV, "0")
    _stub_fetch_ok(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    assert len(results) == 1
    assert results[0].evidence_id == "occ_worktree_unavailable"
    # OMN-17796: SKIPPED check, UNRESOLVED run — see AC1 above.
    assert results[0].status == EnumEvidenceCheckStatus.SKIPPED
    assert collector.occ_ref_failure_cause is (
        EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE
    )
    assert not any(r.evidence_id == "dod-on-dev" for r in results)


@pytest.mark.unit
def test_a_healthy_dev_worktree_is_unaffected_non_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary path still resolves dev-first and records provenance.

    Guards against the refusal being over-broad: with a materialisable
    ``origin/dev`` the run must verify normally and stamp a real 40-char SHA.
    """
    occ = _occ_with_real_dev_ref(tmp_path)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv(_ALLOW_STALE_OCC_REF_ENV, raising=False)
    monkeypatch.delenv(_GIT_OP_TIMEOUT_ENV, raising=False)
    _stub_fetch_ok(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    assert [r.evidence_id for r in results] == ["dod-on-dev"]
    assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
    assert collector.occ_resolved_sha is not None
    assert len(collector.occ_resolved_sha) == 40
