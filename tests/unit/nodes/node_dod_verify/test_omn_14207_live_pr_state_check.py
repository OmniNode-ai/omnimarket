# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-14207: live GitHub PR-state check for PR-bound dod_evidence items.

`node_dod_verify` used to issue a ``verified`` receipt whenever a contract's
declared checks (typically ``grep '^status: PASS$'`` over a static receipt)
passed — WITHOUT ever confirming the cited product PR's live GitHub state. That
is a false-positive class: a receipt can record ``status: PASS`` while the PR is
unmerged / CI-red. Discovery case: ``/onex:dod_verify OMN-13996`` returned
``verified 3/3`` while ``omnibase_infra#2216`` was OPEN with 7 failing required
checks.

These tests prove the fix: for every PR-bound evidence item the collector emits
an ADDITIONAL live-state check (alongside the declared checks) that FAILS when
the PR is not merged, when its checks are not green, or when the live state
cannot be resolved (fail-closed). Non-PR items are unaffected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector as ec_mod
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_SAMPLE_SHA = "1ca9d929aa2aeddd48bbf23f5e2414a67e3bb9e9"
_REPO = "OmniNode-ai/omnibase_infra"
_PR = 2216


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_occ_contract(
    occ_root: Path, ticket_id: str, dod_evidence: list[dict]
) -> Path:
    """Write a contract under ``<occ_root>/contracts/`` and return its path."""
    contracts_dir = occ_root / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": dod_evidence,
    }
    path = contracts_dir / f"{ticket_id}.yaml"
    path.write_text(yaml.dump(contract), encoding="utf-8")
    return path


def _write_receipt(
    occ_root: Path,
    ticket_id: str,
    item_id: str,
    *,
    repo: str = _REPO,
    pr_number: int = _PR,
    status: str = "PASS",
) -> None:
    """Write an OMN-13996-shaped ``command.yaml`` receipt for one evidence item.

    The receipt carries the durable ``pr_number`` + probed ``--repo owner/repo``
    binding — the same fields the DurableEvidenceGate binds against — so the
    collector can resolve the live PR to verify.
    """
    receipt_dir = occ_root / "drift" / "dod_receipts" / ticket_id / item_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "evidence_item_id": item_id,
        "check_type": "command",
        "check_value": (
            f"grep -q '^status: PASS$' "
            f"drift/dod_receipts/{ticket_id}/{item_id}/command.yaml"
        ),
        "status": status,
        "commit_sha": _SAMPLE_SHA,
        "run_timestamp": "2026-07-05T23:25:00Z",
        "runner": "claude",
        "verifier": "claude-review",
        "probe_command": (f"gh pr view {pr_number} --repo {repo} --json number,state"),
        "pr_number": pr_number,
    }
    (receipt_dir / "command.yaml").write_text(yaml.dump(receipt), encoding="utf-8")


def _pr_bound_item(item_id: str = f"dod-omnibase_infra-pr-{_PR}") -> dict:
    """A PR-bound evidence item whose declared check greps its own receipt."""
    return {
        "id": item_id,
        "description": f"omnibase_infra PR #{_PR} carries the migration",
        "checks": [
            {
                "check_type": "command",
                "check_value": (
                    f"grep -q '^status: PASS$' "
                    f"drift/dod_receipts/OMN-14207T/{item_id}/command.yaml"
                ),
            }
        ],
    }


def _install_fetch_mocks(
    collector: EvidenceCollector,
    *,
    merge_result: tuple[bool, str] | None,
    checks_result: tuple[bool, str],
) -> None:
    """Replace the two gh-shelling fetches with deterministic stubs."""

    def _merge(repo: str, pr_number: int) -> tuple[bool, str] | None:
        return merge_result

    def _checks(repo: str, pr_number: int) -> tuple[bool, str]:
        return checks_result

    collector._fetch_pr_merge_state = _merge  # type: ignore[method-assign]
    collector._fetch_pr_checks_green = _checks  # type: ignore[method-assign]


def _live_results(
    results: list[ModelEvidenceCheckResult],
) -> list[ModelEvidenceCheckResult]:
    return [r for r in results if "live-state" in r.evidence_id]


@pytest.fixture
def occ_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """OCC repo root with OMNI_HOME set and CONTRACT_REPO_DIR / live flag clean."""
    occ_root = tmp_path / "onex_change_control"
    occ_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.delenv("DOD_VERIFY_LIVE_PR_CHECK", raising=False)
    return occ_root


# ---------------------------------------------------------------------------
# Live-state outcome matrix (mocked fetches)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLivePrStateMatrix:
    """merged+green -> pass; unmerged -> fail; CI-red -> fail; gh-error -> fail."""

    def test_merged_and_green_passes(self, occ_env: Path) -> None:
        ticket = "OMN-14207T"
        item = _pr_bound_item()
        _write_occ_contract(occ_env, ticket, [item])
        _write_receipt(occ_env, ticket, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(True, "MERGED"),
            checks_result=(True, "all 42 status check(s) green"),
        )
        results = collector.collect(
            ticket, contract_path=str(occ_env / "contracts" / f"{ticket}.yaml")
        )
        live = _live_results(results)
        assert len(live) == 1
        assert live[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert "MERGED" in (live[0].message or "")

    def test_unmerged_fails(self, occ_env: Path) -> None:
        ticket = "OMN-14207T"
        item = _pr_bound_item()
        _write_occ_contract(occ_env, ticket, [item])
        _write_receipt(occ_env, ticket, item["id"])
        collector = EvidenceCollector()
        # Checks happen to be green — the item must still FAIL because the PR is
        # not merged.
        _install_fetch_mocks(
            collector,
            merge_result=(False, "OPEN"),
            checks_result=(True, "all 42 status check(s) green"),
        )
        results = collector.collect(
            ticket, contract_path=str(occ_env / "contracts" / f"{ticket}.yaml")
        )
        live = _live_results(results)
        assert len(live) == 1
        assert live[0].status == EnumEvidenceCheckStatus.FAILED
        assert "not merged" in (live[0].message or "").lower()
        assert "OPEN" in (live[0].message or "")

    def test_ci_red_fails_independently_of_merge(self, occ_env: Path) -> None:
        """A merged PR whose checks are red still FAILS — the CI axis is distinct."""
        ticket = "OMN-14207T"
        item = _pr_bound_item()
        _write_occ_contract(occ_env, ticket, [item])
        _write_receipt(occ_env, ticket, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(True, "MERGED"),
            checks_result=(False, "7 check(s) not green: Lint, CI Summary"),
        )
        results = collector.collect(
            ticket, contract_path=str(occ_env / "contracts" / f"{ticket}.yaml")
        )
        live = _live_results(results)
        assert len(live) == 1
        assert live[0].status == EnumEvidenceCheckStatus.FAILED
        message = (live[0].message or "").lower()
        assert "not green" in message
        assert "not merged" not in message  # merge axis passed; only CI failed

    def test_unresolvable_pr_fails_closed(self, occ_env: Path) -> None:
        """When gh cannot resolve the PR state, the live check fails closed."""
        ticket = "OMN-14207T"
        item = _pr_bound_item()
        _write_occ_contract(occ_env, ticket, [item])
        _write_receipt(occ_env, ticket, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=None,  # gh missing/auth/network/not-found
            checks_result=(True, "unused"),
        )
        results = collector.collect(
            ticket, contract_path=str(occ_env / "contracts" / f"{ticket}.yaml")
        )
        live = _live_results(results)
        assert len(live) == 1
        assert live[0].status == EnumEvidenceCheckStatus.FAILED
        assert "could not resolve" in (live[0].message or "").lower()


# ---------------------------------------------------------------------------
# Non-PR items are unaffected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNonPrItemUnaffected:
    def test_item_without_receipt_or_pr_field_gets_no_live_check(
        self, occ_env: Path
    ) -> None:
        ticket = "OMN-14207T"
        item = {
            "id": "dod-docs",
            "description": "docs updated (no PR binding)",
            "checks": [{"check_type": "command", "command": "true"}],
        }
        _write_occ_contract(occ_env, ticket, [item])
        # No receipt written -> nothing to bind against.
        collector = EvidenceCollector()

        # If a live check WERE emitted, these stubs would fail it; they must not
        # be reached.
        def _boom_merge(repo: str, pr_number: int) -> tuple[bool, str] | None:
            raise AssertionError("live PR check must not run for a non-PR item")

        collector._fetch_pr_merge_state = _boom_merge  # type: ignore[method-assign]

        results = collector.collect(
            ticket, contract_path=str(occ_env / "contracts" / f"{ticket}.yaml")
        )
        assert _live_results(results) == []
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED


# ---------------------------------------------------------------------------
# Regression: the exact OMN-13996 / #2216 false positive is now caught
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOmn13996Regression:
    def test_static_pass_receipt_no_longer_masks_unmerged_pr(
        self, occ_env: Path
    ) -> None:
        """Reproduce OMN-13996: declared grep-the-receipt check passes, but the
        live PR is OPEN -> overall verdict is now FAILED (was ``verified 3/3``)."""
        ticket = "OMN-14207T"
        item = _pr_bound_item()  # id dod-omnibase_infra-pr-2216
        _write_occ_contract(occ_env, ticket, [item])
        # Receipt records status: PASS (as the implementing agent wrote it) —
        # the declared grep check will therefore VERIFY.
        _write_receipt(occ_env, ticket, item["id"], status="PASS")
        collector = EvidenceCollector()
        # Live state mirrors reality at discovery time: OPEN + failing checks.
        _install_fetch_mocks(
            collector,
            merge_result=(False, "OPEN"),
            checks_result=(False, "7 check(s) not green: Lint, CI Summary"),
        )
        results = collector.collect(
            ticket, contract_path=str(occ_env / "contracts" / f"{ticket}.yaml")
        )

        by_id = {r.evidence_id: r for r in results}
        # The declared grep check still passes (the receipt says PASS)...
        assert by_id[item["id"]].status == EnumEvidenceCheckStatus.VERIFIED
        # ...but the live check catches the unmerged / CI-red truth.
        live = by_id[f"{item['id']}::pr-live-state"]
        assert live.status == EnumEvidenceCheckStatus.FAILED

        # And the aggregate verdict is FAILED, not the old ``verified``.
        handler = HandlerDodVerify()
        state = handler.handle(
            {
                "correlation_id": str(uuid4()),
                "ticket_id": ticket,
                "contract_path": str(occ_env / "contracts" / f"{ticket}.yaml"),
                "dry_run": False,
                "requested_at": "2026-07-09T00:00:00+00:00",
            }
        )
        assert isinstance(state, dict)
        assert state["status"] == EnumDodVerifyStatus.FAILED.value
        assert state["failed_count"] >= 1

    def test_handler_overall_failed_when_live_check_fails(self, occ_env: Path) -> None:
        # Confirm the handler collector-path (evidence_results=None) wires through.
        ticket = "OMN-14207T"
        item = _pr_bound_item()
        _write_occ_contract(occ_env, ticket, [item])
        _write_receipt(occ_env, ticket, item["id"])
        # Patch the collector the handler builds so the live fetch is deterministic.
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(False, "OPEN"),
            checks_result=(True, "green"),
        )
        handler = HandlerDodVerify()

        def _return_collector() -> EvidenceCollector:
            return collector

        handler._make_collector = _return_collector  # type: ignore[method-assign]
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )

        cmd = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id=ticket,
            contract_path=str(occ_env / "contracts" / f"{ticket}.yaml"),
            dry_run=False,
            requested_at="2026-07-09T00:00:00+00:00",
        )
        state = handler._handle_typed(cmd)
        assert state.status == EnumDodVerifyStatus.FAILED


# ---------------------------------------------------------------------------
# Binding resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrBindingResolution:
    def test_receipt_derived_binding(self, occ_env: Path) -> None:
        ticket = "OMN-14207T"
        item = _pr_bound_item()
        _write_receipt(occ_env, ticket, item["id"])
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(
            item, ticket, occ_env / "contracts" / f"{ticket}.yaml"
        )
        assert bindings == [(_REPO, _PR)]

    def test_explicit_pr_field_binding(self, occ_env: Path) -> None:
        ticket = "OMN-14207T"
        item = {
            "id": "dod-explicit",
            "description": "explicit binding",
            "pr": {"repo": "omnibase_core", "number": 1234},
            "checks": [{"check_type": "command", "command": "true"}],
        }
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(
            item, ticket, occ_env / "contracts" / f"{ticket}.yaml"
        )
        # Bare repo name is normalized to the OmniNode org.
        assert bindings == [("OmniNode-ai/omnibase_core", 1234)]

    def test_no_binding_for_plain_item(self, occ_env: Path) -> None:
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(
            {"id": "dod-001", "checks": []}, "OMN-14207T", None
        )
        assert bindings == []


# ---------------------------------------------------------------------------
# Env opt-out (deliberate, surfaced as SKIPPED)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLiveCheckDisabled:
    def test_disabled_flag_skips_live_check_but_keeps_verified(
        self, occ_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
        ticket = "OMN-14207T"
        item = _pr_bound_item()
        _write_occ_contract(occ_env, ticket, [item])
        _write_receipt(occ_env, ticket, item["id"])
        collector = EvidenceCollector()

        def _boom(repo: str, pr_number: int) -> tuple[bool, str] | None:
            raise AssertionError("gh must not be shelled when the flag is off")

        collector._fetch_pr_merge_state = _boom  # type: ignore[method-assign]

        results = collector.collect(
            ticket, contract_path=str(occ_env / "contracts" / f"{ticket}.yaml")
        )
        live = _live_results(results)
        assert len(live) == 1
        assert live[0].status == EnumEvidenceCheckStatus.SKIPPED
        assert "disabled" in (live[0].message or "").lower()
        # Item check verified + live skipped -> aggregate stays VERIFIED (the
        # deliberate legacy behaviour when an operator opts out).
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )

        cmd = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id=ticket,
            dry_run=False,
            requested_at="2026-07-09T00:00:00+00:00",
        )
        state = HandlerDodVerify()._handle_typed(cmd, evidence_results=results)
        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert state.skipped_count == 1


# ---------------------------------------------------------------------------
# gh output parsing (subprocess mocked)
# ---------------------------------------------------------------------------


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> object:
    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=list(args[0]) if args else [],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return _run


@pytest.mark.unit
class TestGhOutputParsing:
    def test_merge_state_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ec_mod.subprocess,
            "run",
            _fake_completed('{"mergedAt":"2026-07-01T00:00:00Z","state":"MERGED"}'),
        )
        collector = EvidenceCollector()
        assert collector._fetch_pr_merge_state(_REPO, _PR) == (True, "MERGED")

    def test_merge_state_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ec_mod.subprocess,
            "run",
            _fake_completed('{"mergedAt":null,"state":"OPEN"}'),
        )
        collector = EvidenceCollector()
        assert collector._fetch_pr_merge_state(_REPO, _PR) == (False, "OPEN")

    def test_merge_state_gh_nonzero_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ec_mod.subprocess,
            "run",
            _fake_completed("", returncode=1, stderr="not found"),
        )
        collector = EvidenceCollector()
        assert collector._fetch_pr_merge_state(_REPO, _PR) is None

    def test_checks_all_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = (
            '[{"name":"Lint","state":"SUCCESS"},{"name":"Smoke","state":"SKIPPED"}]'
        )
        monkeypatch.setattr(ec_mod.subprocess, "run", _fake_completed(payload))
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)
        assert green is True
        assert "2 status check(s) green" in detail

    def test_checks_failure_not_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # gh pr checks exits 0 even with FAILURE rows -> states must be parsed.
        payload = (
            '[{"name":"Lint","state":"FAILURE"},'
            '{"name":"CI Summary","state":"SUCCESS"},'
            '{"name":"Build","state":"CANCELLED"}]'
        )
        monkeypatch.setattr(
            ec_mod.subprocess, "run", _fake_completed(payload, returncode=0)
        )
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)
        assert green is False
        assert "not green" in detail
        assert "Lint" in detail
        assert "Build" in detail

    def test_checks_empty_not_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ec_mod.subprocess, "run", _fake_completed("[]"))
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)
        assert green is False
        assert "no status checks" in detail.lower()

    def test_fetch_scopes_to_required_checks_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-14390 regression.

        Discovery case: omnibase_infra#2232 (OMN-14134) had all 14
        branch-protection-required contexts green plus one red *non-required*
        "TODO Audit" check. The old call (no ``--required``) fetched every
        check row and failed the whole gate on that non-required row, while
        mislabeling the result "required checks not green" — a false negative
        that would have wrongly blocked every Done-flip citing that PR.

        This asserts two things: (1) the ``gh pr checks`` invocation actually
        passes ``--required`` (so gh itself never returns non-required rows),
        and (2) given the required-only payload gh would return in that exact
        scenario (all required green; the red non-required row absent because
        gh already filtered it out), the collector reports green.
        """
        captured_args: list[list[str]] = []

        def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            call_args = list(args[0]) if args else []
            captured_args.append(call_args)
            # Mirrors what `gh pr checks --required --json name,state` returns
            # for the #2232 scenario: 14 required contexts, all green. The
            # non-required "TODO Audit" row is absent — gh's own --required
            # filtering never surfaces it here.
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=0,
                stdout=(
                    '[{"name":"CI Summary","state":"SUCCESS"},'
                    '{"name":"verify / verify","state":"SUCCESS"},'
                    '{"name":"deploy-gate / deploy-gate","state":"SUCCESS"}]'
                ),
                stderr="",
            )

        monkeypatch.setattr(ec_mod.subprocess, "run", _run)
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)

        assert captured_args, "gh pr checks was never invoked"
        assert "--required" in captured_args[0], (
            "gh pr checks must be invoked with --required so a red "
            "non-required check (e.g. TODO Audit) can never fail this gate"
        )
        assert green is True
        assert "3 status check(s) green" in detail
