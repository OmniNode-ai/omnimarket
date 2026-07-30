# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15465: the auto-appended ``::pr-live-state`` check must never probe a PR
the evidence item does not name.

Discovery (live, read-only census over ``onex_change_control`` ``origin/dev``
@ ``5a19a5e1b``): **55 non-superseded** dod_evidence items derive a
``(repo, pr_number)`` binding that CONTRADICTS the PR their own ``id``
literally pins, plus 43 more masked behind a contract-entry supersession.

Traced chain for the canonical instance (``contracts/OMN-14551.yaml``):

* item ``occ-self-bind-pr-4711`` — id AND description both say OCC PR #4711;
  its own ``check_value`` is the un-substituted ``gh pr view ${PR_NUMBER}
  --repo ${REPO} ...`` placeholder, so it pins nothing literally;
* receipt ``command.yaml`` — ``pr_number: 4711`` with a matching
  ``probe_command`` (the CORRECT pair);
* receipt ``command.supersede.2424.yaml`` — the OMN-14623 "2nd consumer"
  merged-path supersede written by ``node_occ_companion_compute``, which
  re-binds that prior entry to an unrelated product PR:
  ``pr_number: 2424`` + ``probe_command: gh pr view 2424 --repo
  OmniNode-ai/omnibase_infra``.

``apply_supersessions`` correctly prefers the supersede, and
``_consistent_receipt_repo`` correctly confirms ``omnibase_infra`` for 2424 —
the pair is internally consistent. It is nonetheless the WRONG PR for this
item, so ``occ-self-bind-pr-4711::pr-live-state`` reported live GitHub state
for ``omnibase_infra#2424``. When that carrier PR is merged and green the
check renders **VERIFIED**, and the receipt table reads as proof of PR 4711's
state — a PR the probe never touched. That is a laundering-direction failure,
not a fail-closed one.

This is NOT a regression of the OMN-15382 F2 fix. F2 closed cross-*field*
mixing (a repo scraped from one receipt field paired with ``pr_number`` from
another). Here both halves come from the same field and agree with each other;
they simply describe a different PR than the item does.

Fix under test, both consumer-side in ``_resolve_pr_bindings``:

1. **Contradiction guard (tier 4).** When the item's own ``id`` literally pins
   one or more PR numbers, a receipt-derived binding whose number is not among
   them is REFUSED and surfaced as a visible skip. Absent is honest;
   mis-bound is not.
2. **Same-clause literal coverage (tier 2).** ``repos/<owner>/<repo>/pulls/<N>``
   (the ``gh api`` REST path) and ``https://github.com/<owner>/<repo>/pull/<N>``
   are recognised as same-clause literal pins, so an item that pins its own PR
   inside its own check binds correctly instead of falling through to the
   carrier receipt.

RED-before evidence (run against unmodified ``dev``, this file only):
``test_carrier_rebind_receipt_does_not_bind_item_to_a_pr_its_id_denies`` derived
``[('OmniNode-ai/omnibase_infra', 2424)]`` and
``test_rest_api_pulls_path_is_a_same_clause_literal_pin`` derived ``[]`` with a
``NO_CONSISTENT_PR_BINDING`` note.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)


def _isolate_occ_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point receipt resolution at ``tmp_path`` and nowhere else.

    ``_resolve_contract_repo_dir`` consults ``CONTRACT_REPO_DIR`` then
    ``ONEX_CC_REPO_PATH`` BEFORE ``$OMNI_HOME/onex_change_control``; this
    workspace exports both, and an ambient value silently redirects the lookup
    away from the fixture, turning every assertion below into a vacuous green
    locally while passing in hosted CI (OMN-15390).
    """
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)
    return tmp_path / "onex_change_control"


def _write_receipt(
    occ_root: Path,
    ticket_id: str,
    item_id: str,
    *,
    filename: str,
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
    (receipt_dir / filename).write_text(yaml.dump(receipt), encoding="utf-8")


# The item exactly as it appears in contracts/OMN-14551.yaml: the id and the
# description both name OCC #4711, and the check_value is the un-substituted
# placeholder form, so nothing is literally pinned in the item's own checks.
_PLACEHOLDER_CHECK = "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state"


@pytest.mark.unit
class TestCarrierRebindCannotOverridePrIdentityInTheId:
    def test_carrier_rebind_receipt_does_not_bind_item_to_a_pr_its_id_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OMN-15465 discovery case, reproduced byte-for-byte in shape.

        Pre-fix this derived ``[('OmniNode-ai/omnibase_infra', 2424)]`` — a
        different repo AND a different number than the ``-pr-4711`` the id
        pins. The pair is self-consistent inside the receipt, which is exactly
        why the OMN-15382 F2 consistency check waved it through.
        """
        occ_root = _isolate_occ_root(tmp_path, monkeypatch)
        ticket = "OMN-99465"
        item_id = "occ-self-bind-pr-4711"
        item = {
            "id": item_id,
            "description": "OCC companion PR #4711 — self-bind for OMN-99465.",
            "checks": [{"check_type": "command", "check_value": _PLACEHOLDER_CHECK}],
        }
        # The 2nd-consumer rebind: repo and number agree with each other and
        # describe omnibase_infra#2424, an entirely unrelated product PR.
        _write_receipt(
            occ_root,
            ticket,
            item_id,
            filename="command.yaml",
            check_value=(
                "gh api repos/OmniNode-ai/omnibase_infra/contents/tests/x.py"
                "?ref=ca2a8bafd87a82ee4fcea042ad4b9c28b0ea3122 --jq .content"
            ),
            probe_command=(
                "gh pr view 2424 --repo OmniNode-ai/omnibase_infra "
                "--json number,state,headRefName"
            ),
            pr_number=2424,
        )

        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, ticket, None)

        assert bindings == [], (
            "an item whose id pins PR 4711 must not bind to the carrier PR "
            f"2424; derived {bindings!r}"
        )
        assert collector._last_binding_note is not None
        assert "NO_CONSISTENT_PR_BINDING" in collector._last_binding_note
        # The refusal must name what it refused, or the skip is unactionable.
        assert "4711" in collector._last_binding_note
        assert "2424" in collector._last_binding_note

    def test_refusal_surfaces_as_one_visible_skipped_live_state_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refused != silently dropped: exactly one SKIPPED result is emitted."""
        occ_root = _isolate_occ_root(tmp_path, monkeypatch)
        ticket = "OMN-99466"
        item_id = "dod-omninode_infra-pr-621-head"
        item = {
            "id": item_id,
            "description": "head-SHA probe for omninode_infra#621",
            "checks": [{"check_type": "command", "check_value": _PLACEHOLDER_CHECK}],
        }
        _write_receipt(
            occ_root,
            ticket,
            item_id,
            filename="command.yaml",
            check_value="gh pr view 2551 --repo OmniNode-ai/omnibase_infra --json state",
            probe_command=(
                "gh pr view 2551 --repo OmniNode-ai/omnibase_infra --json state"
            ),
            pr_number=2551,
        )

        collector = EvidenceCollector()
        checks = collector._live_pr_checks_for_item(item, ticket, None)

        assert len(checks) == 1
        assert checks[0].evidence_id == f"{item_id}::pr-live-state"
        assert checks[0].status == EnumEvidenceCheckStatus.SKIPPED
        assert "NO_CONSISTENT_PR_BINDING" in (checks[0].message or "")


@pytest.mark.unit
class TestGuardDoesNotOverRefuse:
    def test_receipt_binding_that_agrees_with_the_id_still_binds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard refuses contradiction, not the receipt tier itself."""
        occ_root = _isolate_occ_root(tmp_path, monkeypatch)
        ticket = "OMN-99467"
        item_id = "occ-self-bind-pr-4711"
        item = {
            "id": item_id,
            "description": "OCC companion PR #4711 — self-bind.",
            "checks": [{"check_type": "command", "check_value": _PLACEHOLDER_CHECK}],
        }
        _write_receipt(
            occ_root,
            ticket,
            item_id,
            filename="command.yaml",
            check_value=(
                "gh pr view 4711 --repo OmniNode-ai/onex_change_control --json state"
            ),
            probe_command=(
                "gh pr view 4711 --repo OmniNode-ai/onex_change_control --json state"
            ),
            pr_number=4711,
        )

        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, ticket, None)

        assert bindings == [("OmniNode-ai/onex_change_control", 4711)]
        assert collector._last_binding_note is None

    def test_id_pinning_no_pr_number_is_unaffected_by_the_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id with no ``-pr-<N>`` token constrains nothing, so the receipt
        tier keeps its pre-fix behaviour (the OMN-15382 F2 contract)."""
        occ_root = _isolate_occ_root(tmp_path, monkeypatch)
        ticket = "OMN-99468"
        item_id = "dod-deploy-assessment"
        item = {
            "id": item_id,
            "description": "no PR number anywhere in the id",
            "checks": [{"check_type": "command", "check_value": "true"}],
        }
        _write_receipt(
            occ_root,
            ticket,
            item_id,
            filename="command.yaml",
            check_value=(
                "gh pr view 5458 --repo OmniNode-ai/onex_change_control --json state"
            ),
            probe_command=(
                "gh pr view 5458 --repo OmniNode-ai/onex_change_control --json state"
            ),
            pr_number=5458,
        )

        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, ticket, None)

        assert bindings == [("OmniNode-ai/onex_change_control", 5458)]
        assert collector._last_binding_note is None

    def test_explicit_pr_field_still_outranks_the_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 1 is a deliberate contract-author declaration and is never
        second-guessed by the id heuristic."""
        _isolate_occ_root(tmp_path, monkeypatch)
        item = {
            "id": "occ-self-bind-pr-4711",
            "description": "explicitly declared elsewhere",
            "pr": {"repo": "OmniNode-ai/omnimarket", "number": 1949},
            "checks": [{"check_type": "command", "check_value": "true"}],
        }
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, "OMN-99469", None)
        assert bindings == [("OmniNode-ai/omnimarket", 1949)]
        assert collector._last_binding_note is None


@pytest.mark.unit
class TestRestApiPullsPathIsALiteralPin:
    def test_rest_api_pulls_path_is_a_same_clause_literal_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``occ-self-bind-pr-5495`` from contracts/OMN-15382.yaml.

        Repo and number sit adjacent in ONE URL inside the item's own check —
        a stronger same-clause guarantee than the ``--repo`` flag form — yet
        pre-fix this derived nothing and emitted a NO_CONSISTENT_PR_BINDING
        skip because the extractor only understood ``gh pr view|checks|diff``.
        """
        _isolate_occ_root(tmp_path, monkeypatch)
        item = {
            "id": "occ-self-bind-pr-5495",
            "description": "OCC PR #5495 — self-bind for the companion repair.",
            "checks": [
                {
                    "check_type": "command",
                    "check_value": (
                        "gh api repos/OmniNode-ai/onex_change_control/pulls/5495/files "
                        "--paginate --jq '.[].filename' | "
                        "grep -qx 'contracts/OMN-15382.yaml'"
                    ),
                }
            ],
        }
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, "OMN-99470", None)
        assert bindings == [("OmniNode-ai/onex_change_control", 5495)]
        assert collector._last_binding_note is None

    def test_github_web_pull_url_is_a_same_clause_literal_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_occ_root(tmp_path, monkeypatch)
        item = {
            "id": "dod-web-url-pin",
            "description": "pins its PR via the web URL form",
            "checks": [
                {
                    "check_type": "command",
                    "check_value": (
                        "curl -sSf https://github.com/OmniNode-ai/omnimarket/pull/1949 "
                        "| grep -q Merged"
                    ),
                }
            ],
        }
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, "OMN-99471", None)
        assert bindings == [("OmniNode-ai/omnimarket", 1949)]
        assert collector._last_binding_note is None

    def test_rest_path_repo_never_pairs_across_a_shell_clause_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same-clause invariant the OMN-15382 F2 fix established must
        hold for the new URL forms too: a repo named in one pipeline stage
        must never pair with a number named in a different one."""
        _isolate_occ_root(tmp_path, monkeypatch)
        item = {
            "id": "dod-cross-clause-guard",
            "description": "repo and number live in different clauses",
            "checks": [
                {
                    "check_type": "command",
                    "check_value": (
                        "gh api repos/OmniNode-ai/omnibase_infra/contents/x.py "
                        "--jq .content && gh pr view 1949 --json state"
                    ),
                }
            ],
        }
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(item, "OMN-99472", None)
        assert bindings == []
