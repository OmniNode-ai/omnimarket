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

import json
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

        # And the aggregate verdict is FAILED, not the old ``verified``. Reuse
        # the already-mocked collector for the handler's dict-shim path too —
        # otherwise handler.handle() builds a fresh, unmocked EvidenceCollector
        # that shells out to the real `gh pr view`/`gh pr checks` for the
        # hardcoded #2216 and flakes as that PR's live state drifts (OMN-14392).
        handler = HandlerDodVerify()

        def _return_collector() -> EvidenceCollector:
            return collector

        handler._make_collector = _return_collector  # type: ignore[method-assign]
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


def _lines(*objs: dict[str, object]) -> str:
    """JSON-lines, matching ``gh api --paginate --jq '....[]'`` stdout."""
    return "\n".join(json.dumps(o) for o in objs)


def _routed_gh(
    *,
    view: str = "",
    view_rc: int = 0,
    protection: str = "",
    protection_rc: int = 0,
    rules: str = "[]",
    rules_rc: int = 0,
    suites: str = "",
    suites_rc: int = 0,
    runs: str = "",
    runs_rc: int = 0,
) -> object:
    """OMN-15709: route the 5-call FETCH_PR_CHECKS_GREEN sequence (``gh pr
    view`` -> classic branch protection -> branch rules -> check-suites ->
    check-runs) to the fixture matching each call's shape. See the sibling
    routing helper in ``test_handler_dod_evidence_github_effect.py`` for the
    canonical, more extensively documented version."""

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        argv = [str(a) for a in (list(args[0]) if args else [])]
        joined = " ".join(argv)
        if "view" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=view_rc, stdout=view, stderr=""
            )
        if "protection/required_status_checks" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=protection_rc, stdout=protection, stderr=""
            )
        if "rules/branches" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=rules_rc, stdout=rules, stderr=""
            )
        if "check-suites" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=suites_rc, stdout=suites, stderr=""
            )
        if "check-runs" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=runs_rc, stdout=runs, stderr=""
            )
        raise AssertionError(
            f"unrouted gh invocation in FETCH_PR_CHECKS_GREEN test: {argv}"
        )

    return _run


def _pr_view_payload(branch: str = "mine") -> str:
    return json.dumps(
        {
            "headRefName": branch,
            "baseRefName": "dev",
            "headRefOid": _SAMPLE_SHA,
        }
    )


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
        # OMN-15709: the old single ``gh pr checks --json name,state`` call
        # was replaced with a head-branch-scoped rollup (see
        # test_handler_dod_evidence_github_effect.py for the full design and
        # the OCC #5745/#5749 regression it fixes) — both required contexts
        # here are produced on the PR's own branch.
        monkeypatch.setattr(
            ec_mod.subprocess,
            "run",
            _routed_gh(
                view=_pr_view_payload(),
                protection=json.dumps(["Lint", "Smoke"]),
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(
                    {
                        "name": "Lint",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "Smoke",
                        "status": "completed",
                        "conclusion": "skipped",
                        "check_suite": {"id": 1},
                    },
                ),
            ),
        )
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)
        assert green is True
        assert "2 required context(s) green" in detail

    def test_checks_failure_not_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A completed run with a failing/cancelled conclusion on the PR's OWN
        # branch must fail closed — same invariant as the pre-OMN-15709
        # ``gh pr checks`` state parsing, now expressed over status+conclusion.
        monkeypatch.setattr(
            ec_mod.subprocess,
            "run",
            _routed_gh(
                view=_pr_view_payload(),
                protection=json.dumps(["Lint", "CI Summary", "Build"]),
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(
                    {
                        "name": "Lint",
                        "status": "completed",
                        "conclusion": "failure",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "CI Summary",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "Build",
                        "status": "completed",
                        "conclusion": "cancelled",
                        "check_suite": {"id": 1},
                    },
                ),
            ),
        )
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)
        assert green is False
        assert "not green" in detail
        assert "Lint" in detail
        assert "Build" in detail

    def test_checks_empty_not_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OMN-15709: the API-surface equivalent of the old 'gh pr checks
        returned an empty array' fail-closed case — the check-runs
        enumeration returns zero rows, so every required context is
        genuinely missing (not merely foreign-only-excluded)."""
        monkeypatch.setattr(
            ec_mod.subprocess,
            "run",
            _routed_gh(
                view=_pr_view_payload(),
                protection=json.dumps(["Lint"]),
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(),
            ),
        )
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)
        assert green is False
        assert "missing entirely" in detail.lower()

    def test_fetch_scopes_to_required_checks_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-14390 regression, re-expressed for the OMN-15709 rewrite.

        Discovery case: omnibase_infra#2232 (OMN-14134) had all
        branch-protection-required contexts green plus one red
        *non-required* "TODO Audit" check. The old call (no ``--required``)
        fetched every check row and failed the whole gate on that
        non-required row. The old fix trusted ``gh pr checks --required``'s
        own server-side filtering; the OMN-15709 rewrite achieves the same
        invariant structurally instead — required-context names are read
        live from branch protection, and the rollup loop only ever looks up
        check-runs BY those names, so a red "TODO Audit" run (not a required
        context) is never even matched, let alone allowed to block.
        """
        monkeypatch.setattr(
            ec_mod.subprocess,
            "run",
            _routed_gh(
                view=_pr_view_payload(),
                protection=json.dumps(
                    ["CI Summary", "verify / verify", "deploy-gate / deploy-gate"]
                ),
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(
                    {
                        "name": "CI Summary",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "verify / verify",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "deploy-gate / deploy-gate",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    },
                    {
                        # Non-required, red, same branch — must never block.
                        "name": "TODO Audit",
                        "status": "completed",
                        "conclusion": "failure",
                        "check_suite": {"id": 1},
                    },
                ),
            ),
        )
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)

        assert green is True, detail
        assert "3 required context(s) green" in detail
