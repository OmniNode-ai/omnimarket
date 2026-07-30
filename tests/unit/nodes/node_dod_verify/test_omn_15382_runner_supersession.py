# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15382 (runner-supersession follow-up): FIX 1 + FIX 2.

FIX 1 — the ``dod_verify`` runner's ``EvidenceCollector`` previously had ZERO
handling of the contract-entry ``evidence_artifact: "supersedes_dod_evidence:
<id>"`` marker (the only prior supersession code, ``durable_evidence_gate.
apply_supersessions``, reads a DIFFERENT surface — receipt files, not contract
entries). A real ``dod_verify OMN-14968`` run therefore reported 4 prose
originals as hard FAILs even though each had a passing ``-rebind-15382``
supersession entry. These tests prove the collector now:

* skips a superseded original's checks entirely (status SUPERSEDED, excluded
  from the failure count) while the superseding entry's checks decide the
  verdict;
* hard-fails (does not silently no-op) a dangling supersession marker whose
  target id does not exist in the contract;
* hard-fails a self-referential marker (and, since OMN-15390 added OCC's
  append-only ordering rule, a FORWARD marker — which makes a cycle
  unrepresentable rather than merely detected);
* resolves a multi-hop chain so only the un-superseded head executes.

FIX 2 — the auto-appended ``::pr-live-state`` check derived a WRONG
(repo, pr_number) pair for a fully-pinned item (discovery case:
``dod-omn-14968-pr-2536-rebind-15382`` derived ``(OmniNode-ai/omnibase_infra,
5458)`` — mixing the receipt's carrier-PR ``pr_number`` field with a repo
extracted from a DIFFERENT PR's probe text — instead of its own literal
``(OmniNode-ai/omnibase_infra, 2536)`` pin). These tests prove the new
fail-closed precedence: (a) a same-clause hardcoded literal pin in the item's
OWN checks; (b) the item id's autobind naming convention; (c) a hardened,
same-field-consistent receipt-derived fallback; else a visible SKIPPED note
instead of a silent omission or a mismatched pair.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)


def _write_contract(
    tmp_path: Path,
    ticket_id: str,
    dod_evidence: list[dict],
) -> Path:
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": dod_evidence,
    }
    p = tmp_path / f"{ticket_id}.yaml"
    p.write_text(yaml.dump(contract), encoding="utf-8")
    return p


def _collect(tmp_path: Path, ticket_id: str, dod_evidence: list[dict]):
    contract_path = _write_contract(tmp_path, ticket_id, dod_evidence)
    collector = EvidenceCollector()
    return collector.collect(ticket_id, contract_path=str(contract_path))


def _write_receipt(
    occ_root: Path,
    ticket_id: str,
    item_id: str,
    *,
    check_value: str,
    probe_command: str,
    pr_number: int,
    status: str = "PASS",
) -> None:
    receipt_dir = occ_root / "drift" / "dod_receipts" / ticket_id / item_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "evidence_item_id": item_id,
        "check_type": "command",
        "check_value": check_value,
        "status": status,
        "commit_sha": "a" * 40,
        "run_timestamp": "2026-07-29T00:00:00Z",
        "runner": "test",
        "verifier": "test-review",
        "probe_command": probe_command,
        "pr_number": pr_number,
    }
    (receipt_dir / "command.yaml").write_text(yaml.dump(receipt), encoding="utf-8")


# ---------------------------------------------------------------------------
# FIX 1: contract-entry supersession
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSupersededOriginalIsSkipped:
    def test_superseded_original_is_not_executed_and_excluded_from_failures(
        self, tmp_path: Path
    ) -> None:
        """RED-first: prove against pre-fix code (git stash) that the ORIGINAL
        prose item hard FAILs with INVALID_CHECK_VALUE_NOT_A_COMMAND even
        though a passing supersession entry exists — the exact OMN-14968
        symptom. Post-fix it must report SUPERSEDED, and the overall verdict
        must not count it as a failure.
        """
        results = _collect(
            tmp_path,
            "OMN-99001",
            [
                {
                    "id": "dod-original",
                    "description": "original (prose, would hard FAIL if run)",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "Recorded product receipt: nothing",
                        }
                    ],
                },
                {
                    "id": "dod-original-rebind",
                    "description": "append-only replacement",
                    "source": "manual",
                    "evidence_artifact": "supersedes_dod_evidence:dod-original",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
            ],
        )
        by_id = {r.evidence_id: r for r in results}
        assert by_id["dod-original"].status == EnumEvidenceCheckStatus.SUPERSEDED
        assert "dod-original-rebind" in (by_id["dod-original"].message or "")
        assert by_id["dod-original-rebind"].status == EnumEvidenceCheckStatus.VERIFIED

        cmd_and_state = _run_handler(results, "OMN-99001")
        assert cmd_and_state.failed_count == 0
        assert cmd_and_state.superseded_count == 1
        assert cmd_and_state.status == EnumDodVerifyStatus.VERIFIED

    def test_superseded_item_gets_no_live_pr_state_check(self, tmp_path: Path) -> None:
        """A superseded item that WOULD bind to a PR (hardcoded literal pin
        in its own check) must not sprout a ``::pr-live-state`` check —
        it is not executed at all.
        """
        results = _collect(
            tmp_path,
            "OMN-99002",
            [
                {
                    "id": "dod-orig-pr",
                    "description": "original PR pin",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": (
                                "gh pr view 42 --repo OmniNode-ai/omnimarket "
                                "--json number,state"
                            ),
                        }
                    ],
                },
                {
                    "id": "dod-orig-pr-rebind",
                    "description": "rebind",
                    "evidence_artifact": "supersedes_dod_evidence:dod-orig-pr",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
            ],
        )
        assert [r.evidence_id for r in results] == [
            "dod-orig-pr",
            "dod-orig-pr-rebind",
        ]


@pytest.mark.unit
class TestDanglingSupersessionIsHardRed:
    def test_marker_pointing_at_nonexistent_id_hard_fails(self, tmp_path: Path) -> None:
        results = _collect(
            tmp_path,
            "OMN-99003",
            [
                {
                    "id": "dod-rebind",
                    "description": "points at an id that was never declared",
                    "evidence_artifact": "supersedes_dod_evidence:dod-does-not-exist",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
            ],
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "DANGLING_SUPERSESSION" in (results[0].message or "")
        assert "dod-does-not-exist" in (results[0].message or "")


@pytest.mark.unit
class TestMalformedSupersessionChainsAreHardRed:
    def test_self_reference_hard_fails(self, tmp_path: Path) -> None:
        results = _collect(
            tmp_path,
            "OMN-99004",
            [
                {
                    "id": "dod-self",
                    "description": "supersedes itself",
                    "evidence_artifact": "supersedes_dod_evidence:dod-self",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
            ],
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "MALFORMED_SUPERSESSION" in (results[0].message or "")

    def test_attempted_two_item_cycle_terminates_under_the_ordering_rule(
        self, tmp_path: Path
    ) -> None:
        """A cycle is unrepresentable once the append-only ordering rule holds.

        OMN-15390 replaced the position-blind whole-contract resolution this
        case was originally written against (which needed an explicit graph
        walk to detect a loop) with OCC's ``if supersedes in seen`` rule, so
        every accepted edge points strictly backwards in declaration order.
        Across the DISTINCT ids used here that also makes the relation acyclic
        — but only because the ids are distinct. ``superseded`` is keyed by id,
        so a contract with a DUPLICATE id still admits an id-level self-loop
        and ``_terminal_superseder``'s visited-set guard is what terminates the
        walk there; see
        ``test_the_visited_set_guard_is_required_not_defensive`` in
        ``test_omn_15390_contract_entry_supersession.py``. The
        mutually-referential contract below therefore resolves
        deterministically in one pass rather than being detected as a cycle:
        ``dod-a``'s marker points FORWARD and
        retires nothing (hard RED on ``dod-a``, matching what the OCC gate
        computes — no supersession); ``dod-b``'s marker points backwards and
        retires ``dod-a`` normally.
        """
        results = _collect(
            tmp_path,
            "OMN-99005",
            [
                {
                    "id": "dod-a",
                    "description": "supersedes b — forward, retires nothing",
                    "evidence_artifact": "supersedes_dod_evidence:dod-b",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
                {
                    "id": "dod-b",
                    "description": "supersedes a — backwards, valid",
                    "evidence_artifact": "supersedes_dod_evidence:dod-a",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
            ],
        )
        by_id = {r.evidence_id: r for r in results}
        # dod-a carries a broken marker, so it is RED on its own account —
        # a malformed marker is reported ahead of any supersession of the
        # same id, so a typo can never masquerade as a quiet skip.
        assert by_id["dod-a"].status == EnumEvidenceCheckStatus.FAILED
        assert "FORWARD_SUPERSESSION" in (by_id["dod-a"].message or "")
        # dod-b's own marker is well-formed, so dod-b executes.
        assert by_id["dod-b"].status == EnumEvidenceCheckStatus.VERIFIED


@pytest.mark.unit
class TestChainResolutionOnlyHeadExecutes:
    def test_multi_hop_chain_supersedes_all_but_the_head(self, tmp_path: Path) -> None:
        results = _collect(
            tmp_path,
            "OMN-99006",
            [
                {
                    "id": "dod-orig",
                    "description": "base",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "Recorded product receipt: nothing",
                        }
                    ],
                },
                {
                    "id": "dod-orig-rebind-1",
                    "description": "first rebind",
                    "evidence_artifact": "supersedes_dod_evidence:dod-orig",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "Recorded product receipt: still bad",
                        }
                    ],
                },
                {
                    "id": "dod-orig-rebind-2",
                    "description": "second rebind — the real head",
                    "evidence_artifact": ("supersedes_dod_evidence:dod-orig-rebind-1"),
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
            ],
        )
        by_id = {r.evidence_id: r for r in results}
        assert by_id["dod-orig"].status == EnumEvidenceCheckStatus.SUPERSEDED
        assert by_id["dod-orig-rebind-1"].status == EnumEvidenceCheckStatus.SUPERSEDED
        assert by_id["dod-orig-rebind-2"].status == EnumEvidenceCheckStatus.VERIFIED

        state = _run_handler(results, "OMN-99006")
        assert state.failed_count == 0
        assert state.superseded_count == 2
        assert state.status == EnumDodVerifyStatus.VERIFIED


def _run_handler(results, ticket_id: str):
    from uuid import uuid4

    from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
        ModelDodVerifyStartCommand,
    )

    cmd = ModelDodVerifyStartCommand(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        dry_run=False,
        requested_at="2026-07-29T00:00:00+00:00",
    )
    return HandlerDodVerify()._handle_typed(cmd, evidence_results=results)


# ---------------------------------------------------------------------------
# FIX 2: ::pr-live-state fail-closed binding derivation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLiveStateBindingFromHardcodedOwnCheck:
    def test_literal_pin_in_own_check_value_wins_over_id_and_receipt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact OMN-14968 discovery case: an item whose id does NOT
        follow the ``dod-<owner>-<repo>-pr-<N>`` convention, but whose OWN
        check_value literally pins ``gh pr view 2536 --repo
        OmniNode-ai/omnibase_infra``, AND whose receipt records a DIFFERENT
        carrier ``pr_number`` (5458) alongside that same repo (the exact
        cross-field mixing shape that produced the wrong pair). The
        hardcoded-own-check source must win, deriving (omnibase_infra, 2536)
        — never (omnibase_infra, 5458).
        """
        occ_root = tmp_path / "onex_change_control"
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
        # OMN-15390: ``_resolve_contract_repo_dir`` consults
        # ``ONEX_CC_REPO_PATH`` BEFORE falling back to
        # ``$OMNI_HOME/onex_change_control``, so an ambient value (this
        # workspace exports one) silently redirects the receipt lookup away
        # from ``tmp_path`` and the assertions below read a vacuous green.
        # Hosted CI has no such value, which is why this only bit locally.
        monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)
        ticket = "OMN-99010"
        item_id = "dod-omn-99010-pr-2536-rebind"
        item = {
            "id": item_id,
            "description": "rebind pinning PR #2536 literally",
            "checks": [
                {
                    "check_type": "command",
                    "check_value": (
                        "gh pr view 2536 --repo OmniNode-ai/omnibase_infra "
                        "--json state --jq .state | grep -qx MERGED"
                    ),
                }
            ],
        }
        # The mismatched receipt: pr_number is the TICKET-CARRIER PR (5458),
        # never mentioned anywhere near the repo in probe_command/check_value.
        _write_receipt(
            occ_root,
            ticket,
            item_id,
            check_value=(
                "gh pr view 2536 --repo OmniNode-ai/omnibase_infra --json state "
                "--jq .state | grep -qx MERGED"
            ),
            probe_command=(
                "gh pr view 2536 --repo OmniNode-ai/omnibase_infra --json state"
            ),
            pr_number=5458,
        )
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, ticket, None)
        assert bindings == [("OmniNode-ai/omnibase_infra", 2536)]
        assert collector._last_binding_note is None

    def test_id_convention_used_when_no_hardcoded_own_check(
        self, tmp_path: Path
    ) -> None:
        collector = EvidenceCollector()
        item = {
            "id": "dod-OmniNode-ai-omnimarket-pr-777",
            "description": "id-convention binding, no literal pin in checks",
            "checks": [
                {"check_type": "command", "check_value": "grep -q x receipt.yaml"}
            ],
        }
        bindings = collector._resolve_pr_bindings(item, "OMN-99011", None)
        assert bindings == [("OmniNode-ai/omnimarket", 777)]
        assert collector._last_binding_note is None


@pytest.mark.unit
class TestLiveStateOmittedAndNotedForUnresolvableBinding:
    def test_cross_field_inconsistent_receipt_yields_note_not_wrong_pair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No explicit fields, no hardcoded own-check pin, id does not follow
        the autobind convention, and the ONLY receipt has a pr_number that is
        never corroborated by the same field as the repo. Must NOT derive the
        mismatched pair — must return no bindings and set a visible note.
        """
        occ_root = tmp_path / "onex_change_control"
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
        # OMN-15390: ``_resolve_contract_repo_dir`` consults
        # ``ONEX_CC_REPO_PATH`` BEFORE falling back to
        # ``$OMNI_HOME/onex_change_control``, so an ambient value (this
        # workspace exports one) silently redirects the receipt lookup away
        # from ``tmp_path`` and the assertions below read a vacuous green.
        # Hosted CI has no such value, which is why this only bit locally.
        monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)
        ticket = "OMN-99012"
        item_id = "dod-unparseable-id"
        item = {
            "id": item_id,
            "description": "no literal pin, no id convention",
            "checks": [{"check_type": "command", "check_value": "true"}],
        }
        _write_receipt(
            occ_root,
            ticket,
            item_id,
            check_value="gh pr view 2536 --repo OmniNode-ai/omnibase_infra --json state",
            probe_command="gh pr view 2536 --repo OmniNode-ai/omnibase_infra --json state",
            pr_number=5458,
        )
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, ticket, None)
        assert bindings == []
        assert collector._last_binding_note is not None
        assert "NO_CONSISTENT_PR_BINDING" in collector._last_binding_note

    def test_live_pr_checks_for_item_surfaces_the_note_visibly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        occ_root = tmp_path / "onex_change_control"
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
        # OMN-15390: ``_resolve_contract_repo_dir`` consults
        # ``ONEX_CC_REPO_PATH`` BEFORE falling back to
        # ``$OMNI_HOME/onex_change_control``, so an ambient value (this
        # workspace exports one) silently redirects the receipt lookup away
        # from ``tmp_path`` and the assertions below read a vacuous green.
        # Hosted CI has no such value, which is why this only bit locally.
        monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)
        ticket = "OMN-99013"
        item_id = "dod-unparseable-id-2"
        item = {
            "id": item_id,
            "description": "no literal pin, no id convention",
            "checks": [{"check_type": "command", "check_value": "true"}],
        }
        _write_receipt(
            occ_root,
            ticket,
            item_id,
            check_value="gh pr view 2536 --repo OmniNode-ai/omnibase_infra --json state",
            probe_command="gh pr view 2536 --repo OmniNode-ai/omnibase_infra --json state",
            pr_number=5458,
        )
        collector = EvidenceCollector()
        checks = collector._live_pr_checks_for_item(item, ticket, None)
        assert len(checks) == 1
        assert checks[0].evidence_id == f"{item_id}::pr-live-state"
        assert checks[0].status == EnumEvidenceCheckStatus.SKIPPED
        assert "NO_CONSISTENT_PR_BINDING" in (checks[0].message or "")

    def test_truly_non_pr_item_stays_silent(self, tmp_path: Path) -> None:
        """Non-regression: an item with zero PR indication at all (no
        explicit fields, no gh-pr checks, no receipt directory) still gets
        no check appended — the note is reserved for a genuine, unresolved
        PR indication, not sprinkled on every unrelated item.
        """
        collector = EvidenceCollector()
        item = {
            "id": "dod-docs",
            "description": "docs only",
            "checks": [{"check_type": "command", "command": "true"}],
        }
        checks = collector._live_pr_checks_for_item(item, "OMN-99014", None)
        assert checks == []
        assert collector._last_binding_note is None
