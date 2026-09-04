# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15454 — OCC ref refresh fails OPEN no longer; it fails CLOSED by default.

``EvidenceCollector._refresh_occ_ref`` used to swallow a failed ``git fetch``
at ``logger.info`` and let the collector proceed against whatever the local
remote-tracking ref already had, while still logging that the run resolved
"dev-first" from a verified-fresh ref. This is the RED-first regression suite
driving the REAL dispatch path (``EvidenceCollector.collect()``, real git
subprocesses, no mocked git) against the falsifiable acceptance criteria on
the ticket:

* AC1 — a genuinely failed fetch refuses by default (does not resolve/verify
  anything against the stale local clone).
* AC2 — provenance (resolved 40-char SHA, requested ref, refresh outcome) is
  recorded on the collector and stamped onto ``ModelDodVerifyState``.
* AC3 — the specific ref-lock race is retried once before declaring failure.
* AC4 — a bare local-branch ``OCC_GOVERNANCE_REF`` (no remote) is unaffected
  — still ``NOT_APPLICABLE``, still resolves normally (non-regression).
* AC5 — the override is named, logged, and marks every result
  un-attributable rather than silently proceeding as if nothing happened.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    EnumOccRefRefreshOutcome,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _ALLOW_STALE_OCC_REF_ENV,
    EvidenceCollector,
)

_TICKET = "OMN-15454-fixture"


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


def _make_local_clone_with_unreachable_origin(tmp_path: Path, ticket: str) -> Path:
    """A local OCC clone whose ``origin`` cannot be fetched.

    ``git fetch origin dev`` against a nonexistent remote path is a real,
    deterministic failure (not the ref-lock class) — the local clone's
    ``dev`` branch already carries the fixture contract, standing in for
    "a fetch failure while the local clone happens to have SOME content
    already" (the AC1 shape: freshness is unknown either way).
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t.co")
    _git(seed, "config", "user.name", "t")
    _git(seed, "checkout", "-q", "-b", "dev")
    _write_contract(seed, ticket, "dod-local-stale")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed dev")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "dev")

    occ = tmp_path / "onex_change_control"
    _git(tmp_path, "clone", "-q", str(remote), str(occ))
    _git(occ, "checkout", "-q", "dev")
    _git(occ, "checkout", "-q", "-b", "main")

    # Break the remote AFTER cloning: origin/dev exists locally, but any
    # future fetch of it fails deterministically.
    _git(occ, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))
    return occ


@pytest.mark.unit
def test_fetch_failure_refuses_by_default_ac1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    occ = _make_local_clone_with_unreachable_origin(tmp_path, _TICKET)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv(_ALLOW_STALE_OCC_REF_ENV, raising=False)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    # Refused, not silently resolved against the local (unverifiable-fresh)
    # clone — never a "verified" table attributed to origin/dev.
    assert len(results) == 1
    assert results[0].evidence_id == "occ_ref_refresh"
    # OMN-17796 re-encoded this refusal: the check is SKIPPED (nothing ran, so
    # nothing failed) and the RUN is UNRESOLVED with a typed cause. Recording
    # it FAILED asserted a red about a ticket whose contract had not been
    # loaded, and the autoclose sweep rendered that as "not all ACs are
    # receipt-proven". The refusal is unchanged — its encoding is not.
    assert results[0].status == EnumEvidenceCheckStatus.SKIPPED
    assert results[0].status != EnumEvidenceCheckStatus.VERIFIED
    assert collector.occ_ref_failure_cause is (
        EnumDodVerifyUnresolvedCause.OCC_REF_REFRESH_FAILED
    )
    assert "OCC_REF_REFRESH_FAILED" in (results[0].message or "")
    assert collector.occ_refresh_outcome == EnumOccRefRefreshOutcome.FETCH_FAILED
    assert not any(
        r.status == EnumEvidenceCheckStatus.VERIFIED
        and r.evidence_id == "dod-local-stale"
        for r in results
    )


@pytest.mark.unit
def test_fetch_failure_override_marks_every_result_unattributable_ac5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    occ = _make_local_clone_with_unreachable_origin(tmp_path, _TICKET)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv(_ALLOW_STALE_OCC_REF_ENV, "1")

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    real_check = next(r for r in results if r.evidence_id == "dod-local-stale")
    assert real_check.status == EnumEvidenceCheckStatus.VERIFIED
    # Disclosed, not buried: the un-attributable marker is on the real
    # result's own message, plus a standalone item names the override.
    assert "UNATTRIBUTABLE" in (real_check.message or "")
    override_item = next(
        r for r in results if r.evidence_id == "occ_ref_refresh_override"
    )
    assert override_item.status == EnumEvidenceCheckStatus.SKIPPED
    assert _ALLOW_STALE_OCC_REF_ENV in (override_item.message or "")


@pytest.mark.unit
def test_ref_lock_race_is_retried_once_ac3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first fetch failing with the ref-lock race, second succeeding, must
    resolve FETCHED and materialise the worktree at the post-fetch tip.

    OMN-16787 fixture repair. This test previously built a repo with a LOCAL
    ``dev`` branch and no remote at all, then asked the collector to resolve
    ``origin/dev``. ``git worktree add --detach origin/dev`` therefore failed
    with ``invalid reference``, and the assertion below only passed because
    the collector silently fell back to the working tree — which carried the
    same contract, since ``main`` was branched off ``dev``. In other words the
    test asserted "materialised at the post-fetch tip" while the worktree was
    never materialised at all; the fail-open it was blind to is exactly the
    defect OMN-16787 closes. The fixture now creates a real remote so
    ``origin/dev`` genuinely exists and the assertion means what it says.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t.co")
    _git(seed, "config", "user.name", "t")
    _git(seed, "checkout", "-q", "-b", "dev")
    _write_contract(seed, _TICKET, "dod-post-fetch-tip")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "dev contract")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "dev")

    occ = tmp_path / "onex_change_control"
    _git(tmp_path, "clone", "-q", str(remote), str(occ))
    _git(occ, "checkout", "-q", "-b", "main")

    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    # The fetch attempts themselves are stubbed: a real concurrent git
    # ref-lock race is not reproducible hermetically without genuine
    # concurrency, so this drives the SAME retry code path with a controlled
    # sequence of outcomes instead of mocking away the code under test. The
    # worktree add that follows is real, against a real origin/dev.
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)

    attempts: list[int] = []

    def _fake_run_occ_fetch(
        self: EvidenceCollector, occ_path: Path, remote: str, branch: str
    ) -> tuple[EnumOccRefRefreshOutcome, str]:
        attempts.append(1)
        if len(attempts) == 1:
            return (
                EnumOccRefRefreshOutcome.FETCH_FAILED,
                "error: cannot lock ref 'refs/remotes/origin/dev': is at "
                "aaaa111 but expected bbbb222",
            )
        return EnumOccRefRefreshOutcome.FETCHED, ""

    monkeypatch.setattr(EvidenceCollector, "_run_occ_fetch", _fake_run_occ_fetch)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    assert len(attempts) == 2, "must retry exactly once on the ref-lock race"
    assert collector.occ_refresh_outcome == EnumOccRefRefreshOutcome.FETCHED
    assert len(results) == 1
    assert results[0].evidence_id == "dod-post-fetch-tip"
    assert results[0].status == EnumEvidenceCheckStatus.VERIFIED


@pytest.mark.unit
def test_non_ref_lock_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ref-lock failure (offline, no remote) must NOT waste a retry."""
    occ = tmp_path / "onex_change_control"
    occ.mkdir()
    _git(occ, "init", "-q")
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)

    attempts: list[int] = []

    def _fake_run_occ_fetch(
        self: EvidenceCollector, occ_path: Path, remote: str, branch: str
    ) -> tuple[EnumOccRefRefreshOutcome, str]:
        attempts.append(1)
        return (
            EnumOccRefRefreshOutcome.FETCH_FAILED,
            "fatal: could not read from remote",
        )

    monkeypatch.setattr(EvidenceCollector, "_run_occ_fetch", _fake_run_occ_fetch)

    collector = EvidenceCollector()
    collector.collect(_TICKET)

    assert len(attempts) == 1, "a non-retriable failure class must not retry"


@pytest.mark.unit
def test_provenance_resolved_sha_matches_worktree_head_ac2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    occ = tmp_path / "onex_change_control"
    occ.mkdir()
    _git(occ, "init", "-q")
    _git(occ, "config", "user.email", "t@t.co")
    _git(occ, "config", "user.name", "t")
    _git(occ, "checkout", "-q", "-b", "main")
    (occ / "README.md").write_text("root\n", encoding="utf-8")
    _git(occ, "add", "-A")
    _git(occ, "commit", "-q", "-m", "init")

    _git(occ, "checkout", "-q", "-b", "dev")
    _write_contract(occ, _TICKET, "dod-provenance")
    _git(occ, "add", "-A")
    _git(occ, "commit", "-q", "-m", "dev contract")
    dev_sha = subprocess.run(
        ["git", "-C", str(occ), "rev-parse", "dev"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(occ, "checkout", "-q", "main")

    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv(
        "OCC_GOVERNANCE_REF", "dev"
    )  # local branch: NOT_APPLICABLE (AC4)
    monkeypatch.delenv("OMNI_HOME", raising=False)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET)

    assert collector.occ_refresh_outcome == EnumOccRefRefreshOutcome.NOT_APPLICABLE
    assert collector.occ_resolved_sha == dev_sha
    assert len(dev_sha) == 40
    assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    # And the same provenance is stamped onto the emitted state (handler
    # boundary), not just readable off the collector instance directly.
    handler = HandlerDodVerify()
    command = ModelDodVerifyStartCommand(ticket_id=_TICKET)
    state = handler.handle(command)
    assert state.occ_governance_ref == "dev"
    assert state.occ_refresh_outcome == EnumOccRefRefreshOutcome.NOT_APPLICABLE
    assert state.occ_resolved_sha == dev_sha


@pytest.mark.unit
def test_explicit_contract_path_never_attempts_occ_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4 corollary: an explicit contract_path skips OCC auto-resolution
    entirely, so provenance stays None rather than a stale/fabricated value.
    """
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(
        "schema_version: '1.0.0'\n"
        f"ticket_id: {_TICKET}\n"
        "dod_evidence:\n"
        "  - id: dod-explicit\n"
        "    description: trivially true\n"
        "    checks:\n"
        "      - check_type: command\n"
        "        check_value: 'true'\n",
        encoding="utf-8",
    )
    collector = EvidenceCollector()
    collector.collect(_TICKET, contract_path=str(contract_path))

    assert collector.occ_refresh_outcome is None
    assert collector.occ_resolved_sha is None
