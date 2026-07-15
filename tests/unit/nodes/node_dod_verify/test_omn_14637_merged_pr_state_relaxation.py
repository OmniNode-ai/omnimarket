# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-14637: merged-state re-anchoring of self-referential live-OPEN gates.

``node_dod_verify`` re-executes every contract ``command``/``check_value`` check
literally. When a ticket's DoD evidence asserts its product PR is still live-OPEN
(the canonical ``gh pr view <n> --repo <r> --json state,... --jq '.state ==
"OPEN" and ...'`` idiom), the assertion becomes PERMANENTLY false the moment the
PR squash-merges — GitHub flips ``.state`` to MERGED and deletes the head branch.
The sanctioned closeout then fails-closed forever on a normal, successful merge
(13/26 checks failed re-running ``dod_verify OMN-11878`` post-merge).

The fix teaches the collector to detect "product PR is now MERGED" (via the SAME
authoritative binding + live-probe machinery the OMN-14207 live-state check uses)
and re-anchor ONLY the ``.state == "OPEN"`` equality to the merged terminal state
(``.state == "MERGED"``). Every other predicate still runs against the merged
PR's retained content, so a genuinely-incomplete ticket STILL FAILS — the
relaxation is verification-preserving and non-vacuous.

These tests prove:
* the identical command that FAILS run verbatim (old behaviour, PR merged) PASSES
  once the merged-state relaxation is applied (RED → GREEN on the same artifact);
* a genuinely-unmet content assertion STILL FAILS even for a merged PR
  (non-vacuous / prove-RED-against-exists-but-wrong);
* the predicate rewrite is surgical — only ``.state``'s ``OPEN`` equality moves,
  never an unrelated ``"OPEN"`` literal such as ``.title == "OPEN"``;
* the relaxation is gated on a CONFIRMED-MERGED live probe — an OPEN/unresolved
  PR, an unbound item, or a disabled live check all run the command verbatim.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_SAMPLE_SHA = "99d0d971b3add6c831afd930fe114d5f0949e13b"
_REPO = "OmniNode-ai/onex_change_control"
_PR = 4163
_TICKET = "OMN-14637T"

# A command that ONLY passes once the merged-state relaxation rewrites the literal
# ``.state == "OPEN"`` to ``.state == "MERGED"``: the echoed text is grepped for
# ``MERGED``. Run verbatim (PR still live-OPEN in the check's eyes) it FAILS; run
# relaxed (PR confirmed MERGED) it PASSES. No ``gh``/``jq`` needed — hermetic.
_CMD_PASS_IF_RELAXED = "echo '.state == \"OPEN\"' | grep -q MERGED"

# Same shape, but the content predicate greps for a marker that is genuinely
# absent. Even after OPEN → MERGED, this must FAIL — the relaxation does not
# conjure content that is not there.
_CMD_ABSENT_CONTENT = "echo '.state == \"OPEN\"' | grep -q ABSENT_CONTENT_MARKER"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_occ_contract(
    occ_root: Path, ticket_id: str, dod_evidence: list[dict]
) -> Path:
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
) -> None:
    """Write a durable receipt binding an evidence item to a product PR.

    Carries the same ``pr_number`` + probed ``--repo owner/repo`` fields the
    DurableEvidenceGate / OMN-14207 live check bind against, so the collector can
    resolve the product PR to probe its merge state.
    """
    receipt_dir = occ_root / "drift" / "dod_receipts" / ticket_id / item_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "evidence_item_id": item_id,
        "check_type": "command",
        "check_value": _CMD_PASS_IF_RELAXED,
        "status": "PASS",
        "commit_sha": _SAMPLE_SHA,
        "run_timestamp": "2026-07-15T03:19:00Z",
        "runner": "claude",
        "verifier": "claude-review",
        "probe_command": f"gh pr view {pr_number} --repo {repo} --json number,state",
        "pr_number": pr_number,
    }
    (receipt_dir / "command.yaml").write_text(yaml.dump(receipt), encoding="utf-8")


def _pr_bound_item(item_id: str, command: str) -> dict:
    return {
        "id": item_id,
        "description": "product PR carries the reviewed content",
        "checks": [{"check_type": "command", "check_value": command}],
    }


def _install_fetch_mocks(
    collector: EvidenceCollector,
    *,
    merge_result: tuple[bool, str] | None,
    checks_result: tuple[bool, str] = (True, "green"),
) -> None:
    def _merge(repo: str, pr_number: int) -> tuple[bool, str] | None:
        return merge_result

    def _checks(repo: str, pr_number: int) -> tuple[bool, str]:
        return checks_result

    collector._fetch_pr_merge_state = _merge  # type: ignore[method-assign]
    collector._fetch_pr_checks_green = _checks  # type: ignore[method-assign]


@pytest.fixture
def occ_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    occ_root = tmp_path / "onex_change_control"
    occ_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.delenv("DOD_VERIFY_LIVE_PR_CHECK", raising=False)
    return occ_root


# ---------------------------------------------------------------------------
# RED → GREEN on the identical artifact (the core of the bug + fix)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerbatimRedRelaxedGreen:
    def test_identical_command_fails_verbatim_but_passes_relaxed(self) -> None:
        """The exact failure/fix pair on ONE command.

        ``relax_merged_state=False`` reproduces the pre-fix behaviour: the command
        runs verbatim, its ``.state == "OPEN"`` echo never contains ``MERGED``, and
        the check FAILS (this is the permanent post-merge failure OMN-14637
        describes). ``relax_merged_state=True`` re-anchors the gate and the SAME
        command PASSES.
        """
        collector = EvidenceCollector()
        check = {"check_type": "command", "check_value": _CMD_PASS_IF_RELAXED}

        ok_verbatim, msg_verbatim = collector._run_command_check(
            check, _TICKET, None, relax_merged_state=False
        )
        assert ok_verbatim is False, msg_verbatim

        ok_relaxed, msg_relaxed = collector._run_command_check(
            check, _TICKET, None, relax_merged_state=True
        )
        assert ok_relaxed is True, msg_relaxed

    def test_relaxation_is_non_vacuous_unmet_content_still_fails(self) -> None:
        """prove-RED-against-exists-but-wrong: relaxing OPEN → MERGED does NOT make
        a check pass whose content assertion is genuinely unmet. The merged PR is
        real, the relaxation fires, yet the grep for an absent marker still FAILS.
        """
        collector = EvidenceCollector()
        check = {"check_type": "command", "check_value": _CMD_ABSENT_CONTENT}
        ok, msg = collector._run_command_check(
            check, _TICKET, None, relax_merged_state=True
        )
        assert ok is False, msg


# ---------------------------------------------------------------------------
# Predicate-rewrite precision (pure string transform)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRelaxPredicatePrecision:
    def test_double_quoted_state_predicate_relaxed(self) -> None:
        cmd = '.state == "OPEN" and .baseRefName == "dev"'
        out, changed = EvidenceCollector._relax_merged_pr_state_predicate(cmd)
        assert changed is True
        assert out == '.state == "MERGED" and .baseRefName == "dev"'

    def test_single_quoted_state_predicate_relaxed_preserving_quotes(self) -> None:
        cmd = ".state == 'OPEN' and .foo == 1"
        out, changed = EvidenceCollector._relax_merged_pr_state_predicate(cmd)
        assert changed is True
        assert out == ".state == 'MERGED' and .foo == 1"

    def test_realistic_dod001_jq_only_state_moves(self) -> None:
        cmd = (
            "gh pr view 4163 --repo OmniNode-ai/onex_change_control "
            "--json state,baseRefName,headRefOid,files,title "
            '--jq \'.state == "OPEN" and .baseRefName == "dev" and '
            '.headRefOid == "abc" and (.title | contains("OMN-11878"))\' '
            "| grep -qx true"
        )
        out, changed = EvidenceCollector._relax_merged_pr_state_predicate(cmd)
        assert changed is True
        assert '.state == "MERGED"' in out
        # Every other predicate is untouched.
        assert '.baseRefName == "dev"' in out
        assert '.headRefOid == "abc"' in out
        assert 'contains("OMN-11878")' in out
        # The only OPEN token that moved was the .state one.
        assert '"OPEN"' not in out

    def test_unrelated_open_literal_is_not_touched(self) -> None:
        cmd = '.title == "OPEN" and .body == "OPEN"'
        out, changed = EvidenceCollector._relax_merged_pr_state_predicate(cmd)
        assert changed is False
        assert out == cmd

    def test_non_open_state_predicate_is_not_touched(self) -> None:
        cmd = '.state == "CLOSED"'
        out, changed = EvidenceCollector._relax_merged_pr_state_predicate(cmd)
        assert changed is False
        assert out == cmd

    def test_no_state_predicate_unchanged(self) -> None:
        cmd = "grep -q '^status: PASS$' command.yaml"
        out, changed = EvidenceCollector._relax_merged_pr_state_predicate(cmd)
        assert changed is False
        assert out == cmd


# ---------------------------------------------------------------------------
# Merged-binding gate (authoritative, live-probe, fail-safe)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMergedBindingGate:
    def test_true_when_bound_pr_is_merged(self, occ_env: Path) -> None:
        item = _pr_bound_item("dod-001", _CMD_PASS_IF_RELAXED)
        _write_receipt(occ_env, _TICKET, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(collector, merge_result=(True, "MERGED"))
        contract_path = occ_env / "contracts" / f"{_TICKET}.yaml"
        assert collector._item_bound_to_merged_pr(item, _TICKET, contract_path) is True

    def test_false_when_bound_pr_is_open(self, occ_env: Path) -> None:
        item = _pr_bound_item("dod-001", _CMD_PASS_IF_RELAXED)
        _write_receipt(occ_env, _TICKET, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(collector, merge_result=(False, "OPEN"))
        contract_path = occ_env / "contracts" / f"{_TICKET}.yaml"
        assert collector._item_bound_to_merged_pr(item, _TICKET, contract_path) is False

    def test_false_when_probe_unresolved(self, occ_env: Path) -> None:
        item = _pr_bound_item("dod-001", _CMD_PASS_IF_RELAXED)
        _write_receipt(occ_env, _TICKET, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(collector, merge_result=None)  # gh missing/auth/etc
        contract_path = occ_env / "contracts" / f"{_TICKET}.yaml"
        assert collector._item_bound_to_merged_pr(item, _TICKET, contract_path) is False

    def test_false_for_unbound_item(self, occ_env: Path) -> None:
        item = _pr_bound_item("dod-docs", "true")  # no receipt, no explicit pr
        collector = EvidenceCollector()

        def _boom(repo: str, pr_number: int) -> tuple[bool, str] | None:
            raise AssertionError("merge state must not be probed for an unbound item")

        collector._fetch_pr_merge_state = _boom  # type: ignore[method-assign]
        contract_path = occ_env / "contracts" / f"{_TICKET}.yaml"
        assert collector._item_bound_to_merged_pr(item, _TICKET, contract_path) is False

    def test_false_when_live_check_disabled(
        self, occ_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
        item = _pr_bound_item("dod-001", _CMD_PASS_IF_RELAXED)
        _write_receipt(occ_env, _TICKET, item["id"])
        collector = EvidenceCollector()

        def _boom(repo: str, pr_number: int) -> tuple[bool, str] | None:
            raise AssertionError("gh must not be probed when the live check is off")

        collector._fetch_pr_merge_state = _boom  # type: ignore[method-assign]
        contract_path = occ_env / "contracts" / f"{_TICKET}.yaml"
        assert collector._item_bound_to_merged_pr(item, _TICKET, contract_path) is False


# ---------------------------------------------------------------------------
# End-to-end wiring through collect() / the handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollectEndToEnd:
    def test_merged_bound_item_declared_check_verified(self, occ_env: Path) -> None:
        """The exact OMN-14637 scenario: a merged product PR whose declared
        live-OPEN-state check would fail verbatim is VERIFIED after relaxation,
        wired end-to-end through ``collect()``.
        """
        item = _pr_bound_item("dod-001", _CMD_PASS_IF_RELAXED)
        contract_path = _write_occ_contract(occ_env, _TICKET, [item])
        _write_receipt(occ_env, _TICKET, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(True, "MERGED"),
            checks_result=(True, "all 42 status check(s) green"),
        )
        results = collector.collect(_TICKET, contract_path=str(contract_path))
        by_id = {r.evidence_id: r for r in results}
        assert by_id["dod-001"].status == EnumEvidenceCheckStatus.VERIFIED
        # The live-state check also passes (merged + green) → aggregate VERIFIED.
        handler = HandlerDodVerify()
        cmd = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id=_TICKET,
            dry_run=False,
            requested_at="2026-07-15T00:00:00+00:00",
        )
        state = handler._handle_typed(cmd, evidence_results=results)
        assert state.status == EnumDodVerifyStatus.VERIFIED

    def test_open_bound_item_declared_check_fails_verbatim(self, occ_env: Path) -> None:
        """Control: while the product PR is still OPEN, the SAME declared check
        runs verbatim (no relaxation) and FAILS — the fix does not weaken
        verification for unmerged PRs.
        """
        item = _pr_bound_item("dod-001", _CMD_PASS_IF_RELAXED)
        contract_path = _write_occ_contract(occ_env, _TICKET, [item])
        _write_receipt(occ_env, _TICKET, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(collector, merge_result=(False, "OPEN"))
        results = collector.collect(_TICKET, contract_path=str(contract_path))
        by_id = {r.evidence_id: r for r in results}
        assert by_id["dod-001"].status == EnumEvidenceCheckStatus.FAILED

    def test_merged_bound_item_unmet_content_still_fails_end_to_end(
        self, occ_env: Path
    ) -> None:
        """Non-vacuous end-to-end: a merged PR whose declared check asserts absent
        content still FAILS even though the OPEN → MERGED relaxation fires.
        """
        item = _pr_bound_item("dod-001", _CMD_ABSENT_CONTENT)
        contract_path = _write_occ_contract(occ_env, _TICKET, [item])
        _write_receipt(occ_env, _TICKET, item["id"])
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(True, "MERGED"),
            checks_result=(True, "green"),
        )
        results = collector.collect(_TICKET, contract_path=str(contract_path))
        by_id = {r.evidence_id: r for r in results}
        assert by_id["dod-001"].status == EnumEvidenceCheckStatus.FAILED
