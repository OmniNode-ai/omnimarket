# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13888 — durable-evidence tombstone honoring + product contract resolution.

* apply_supersessions drops tombstoned base receipts and substitutes replacements.
* DurableEvidenceGate Check 2 fails a closed-unmerged citation and passes once a
  supersession re-binds the key to the merged PR (the OMN-13899 acceptance case).
* EvidenceCollector resolves contracts from merged product-repository PRs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDurableEvidenceCheck,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    DEFAULT_OCC_GOVERNANCE_REF,
    DurableEvidenceGate,
    apply_supersessions,
    default_receipt_dir,
    extract_receipt_merge_commits,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import World, contract

_OCC_REPO = "/fake/onex_change_control"
_DEV_REF = DEFAULT_OCC_GOVERNANCE_REF
_TICKET = "OMN-13899"
_RECEIPT_DIR = default_receipt_dir(_TICKET)
_REPO = "OmniNode-ai/omniclaude"
_MERGED_SHA = "da461b241024c0299878cc45b156345b1c171d2e"


def _receipt(*, pr_number: int, commit_sha: str, status: str = "PASS") -> dict:
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "evidence_item_id": "dod-omniclaude-pr",
        "check_type": "command",
        "check_value": "true",
        "status": status,
        "run_timestamp": "2026-07-03T15:00:00Z",
        "commit_sha": commit_sha,
        "runner": "worker",
        "verifier": "reviewer",
        "probe_command": f"gh pr view {pr_number} --repo {_REPO} --json number,url,state",
        "probe_stdout": (
            f'{{"number":{pr_number},"url":"https://github.com/{_REPO}/pull/'
            f'{pr_number}","state":"MERGED"}}'
        ),
        "pr_number": pr_number,
    }


def _supersession(
    *,
    created_at: str,
    tombstone: bool,
    replacement: dict | None,
    source_name: str | None = None,
) -> dict:
    data = {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "evidence_item_id": "dod-omniclaude-pr",
        "check_type": "command",
        "supersedes": f"drift/dod_receipts/{_TICKET}/dod-omniclaude-pr/command.yaml",
        "reason": "rebind to the actually-merged PR #1846",
        "superseder": "closeout-agent",
        "created_at": created_at,
        "tombstone": tombstone,
    }
    if replacement is not None:
        data["replacement"] = replacement
    if source_name is not None:
        # OMN-13888: the loader attaches the receipt basename so apply_supersessions
        # can order by the unforgeable .supersede.<NNNN> ordinal (matches core).
        data["__source_name__"] = source_name
    return data


# --------------------------------------------------------------------------- #
# apply_supersessions
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_apply_supersessions_drops_tombstoned_base() -> None:
    base = _receipt(pr_number=1845, commit_sha="a" * 40)
    tomb = _supersession(
        created_at="2026-07-03T16:00:00Z", tombstone=True, replacement=None
    )
    resolved = apply_supersessions([base, tomb])
    assert resolved == []


@pytest.mark.unit
def test_apply_supersessions_substitutes_replacement() -> None:
    base = _receipt(pr_number=1845, commit_sha="a" * 40)
    replacement = _receipt(pr_number=1846, commit_sha=_MERGED_SHA)
    record = _supersession(
        created_at="2026-07-03T16:00:00Z", tombstone=False, replacement=replacement
    )
    resolved = apply_supersessions([base, record])
    assert len(resolved) == 1
    assert resolved[0]["pr_number"] == 1846


@pytest.mark.unit
def test_apply_supersessions_latest_created_at_wins() -> None:
    base = _receipt(pr_number=1845, commit_sha="a" * 40)
    early_tomb = _supersession(
        created_at="2026-07-03T16:00:00Z", tombstone=True, replacement=None
    )
    late_rebind = _supersession(
        created_at="2026-07-03T18:00:00Z",
        tombstone=False,
        replacement=_receipt(pr_number=1846, commit_sha=_MERGED_SHA),
    )
    resolved = apply_supersessions([base, early_tomb, late_rebind])
    assert len(resolved) == 1
    assert resolved[0]["pr_number"] == 1846


@pytest.mark.unit
def test_apply_supersessions_filename_ordinal_beats_created_at() -> None:
    """OMN-13888 consistency: the .supersede.<NNNN> filename ordinal (the same
    authority key omnibase_core resolve_supersession uses) decides "latest",
    NOT the attacker-controllable created_at. Here the LATER created_at carries
    the LOWER ordinal, so it must LOSE to the higher-ordinal record.
    """
    base = _receipt(pr_number=1845, commit_sha="a" * 40)
    # Higher ordinal (0002) but EARLIER created_at → must win (rebind to #1846).
    winner = _supersession(
        created_at="2026-07-03T16:00:00Z",
        tombstone=False,
        replacement=_receipt(pr_number=1846, commit_sha=_MERGED_SHA),
        source_name="command.supersede.0002.yaml",
    )
    # Lower ordinal (0001) but LATER created_at → must lose despite newer stamp.
    loser = _supersession(
        created_at="2026-07-03T23:00:00Z",
        tombstone=True,
        replacement=None,
        source_name="command.supersede.0001.yaml",
    )
    resolved = apply_supersessions([base, loser, winner])
    assert len(resolved) == 1
    assert resolved[0]["pr_number"] == 1846


@pytest.mark.unit
def test_apply_supersessions_numbered_record_beats_unnumbered() -> None:
    """A record carrying a filename ordinal always outranks a bare payload that
    has none, regardless of created_at."""
    base = _receipt(pr_number=1845, commit_sha="a" * 40)
    numbered_tomb = _supersession(
        created_at="2026-07-03T10:00:00Z",
        tombstone=True,
        replacement=None,
        source_name="command.supersede.0005.yaml",
    )
    unnumbered_rebind = _supersession(
        created_at="2026-07-03T23:59:00Z",
        tombstone=False,
        replacement=_receipt(pr_number=1846, commit_sha=_MERGED_SHA),
    )
    resolved = apply_supersessions([base, unnumbered_rebind, numbered_tomb])
    # The numbered tombstone wins → base dropped, no active receipt.
    assert resolved == []


# --------------------------------------------------------------------------- #
# DurableEvidenceGate Check 2 — acceptance case
# --------------------------------------------------------------------------- #


# OMN-18010: the released-is-Done probes are REQUIRED gate constructor
# arguments — there is no unconfigured/skip path. These stubs answer "released"
# for every input, so the pre-OMN-18010 behaviour of the cases in this file is
# unchanged and the new check is exercised on its own terms in
# tests/unit/nodes/node_dod_verify/test_omn_18010_released_gate.py.
def _released_tags_stub(
    table: dict[tuple[str, str], tuple[str, ...] | None] | None = None,
):
    def probe(repo: str, commit_sha: str) -> tuple[str, ...] | None:
        if table is not None and (repo, commit_sha) in table:
            return table[(repo, commit_sha)]
        return ("v9.9.9",)

    return probe


def _released_index_stub(
    table: dict[tuple[str, str], frozenset[str] | None] | None = None,
):
    def probe(distribution: str, version: str) -> frozenset[str] | None:
        if table is not None and (distribution, version) in table:
            return table[(distribution, version)]
        return frozenset({"bdist_wheel", "sdist"})

    return probe


def _make_gate(
    *,
    pr_view: dict,
    receipts: list[dict],
    released_tags: dict[tuple[str, str], tuple[str, ...] | None] | None = None,
    released_index: dict[tuple[str, str], frozenset[str] | None] | None = None,
) -> DurableEvidenceGate:
    def is_receipt_tracked(repo_path: str, ref: str, receipt_dir: str) -> bool:
        return True

    def gh_pr_view(repo: str, pr_number: int) -> tuple[str, str | None]:
        return pr_view[(repo, pr_number)]

    # These acceptance cases bind via merge-commit identity (or fail at the
    # state check), so the PR-commits membership leg (OMN-14255) is never
    # consulted — an empty probe keeps them fail-closed and untouched.
    def pr_commits(repo: str, pr_number: int) -> tuple[str, ...]:
        return ()

    def load_contract(repo_path: str, ref: str, rel_path: str) -> dict | None:
        return {
            "schema_version": "1.0.0",
            "ticket_id": _TICKET,
            "dod_evidence": [
                {
                    "id": "dod-omniclaude-pr",
                    "description": "x",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                }
            ],
        }

    def load_receipts(repo_path: str, ref: str, receipt_dir: str) -> list[dict]:
        return receipts

    return DurableEvidenceGate(
        release_tags_containing=_released_tags_stub(released_tags),
        index_release_files=_released_index_stub(released_index),
        is_receipt_tracked=is_receipt_tracked,
        gh_pr_view=gh_pr_view,
        pr_commits=pr_commits,
        load_contract_on_ref=load_contract,
        load_receipts_on_ref=load_receipts,
        occ_repo_path=_OCC_REPO,
        occ_governance_ref=_DEV_REF,
    )


def _check2(result) -> object:
    return next(
        c
        for c in result.checks
        if c.check == EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT
    )


@pytest.mark.unit
def test_closed_unmerged_citation_fails_check2() -> None:
    gate = _make_gate(
        pr_view={(_REPO, 1845): ("CLOSED", None)},
        receipts=[_receipt(pr_number=1845, commit_sha="a" * 40)],
    )
    contract = gate._load_contract_on_ref(_OCC_REPO, _DEV_REF, "x")
    result = gate.evaluate_default(
        ticket_id=_TICKET, contract=contract, ticket_labels=frozenset()
    )
    assert _check2(result).passed is False


@pytest.mark.unit
def test_supersession_rebind_passes_check2_on_merged_pr() -> None:
    base = _receipt(pr_number=1845, commit_sha="a" * 40)
    replacement = _receipt(pr_number=1846, commit_sha=_MERGED_SHA)
    tomb_rebind = _supersession(
        created_at="2026-07-03T18:00:00Z", tombstone=False, replacement=replacement
    )
    gate = _make_gate(
        pr_view={
            (_REPO, 1846): ("MERGED", _MERGED_SHA),
            # #1845 must NOT be probed — it was superseded/dropped.
        },
        receipts=[base, tomb_rebind],
    )
    contract = gate._load_contract_on_ref(_OCC_REPO, _DEV_REF, "x")
    result = gate.evaluate_default(
        ticket_id=_TICKET, contract=contract, ticket_labels=frozenset()
    )
    assert _check2(result).passed is True, _check2(result).message


@pytest.mark.unit
def test_tombstone_only_yields_no_citation() -> None:
    base = _receipt(pr_number=1845, commit_sha="a" * 40)
    tomb = _supersession(
        created_at="2026-07-03T18:00:00Z", tombstone=True, replacement=None
    )
    assert extract_receipt_merge_commits(apply_supersessions([base, tomb])) == []


# --------------------------------------------------------------------------- #
# EvidenceCollector resolves merged product contracts
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_evidence_collector_resolves_product_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path)
    world.merge(
        "omnimarket",
        11,
        f"feat({_TICKET}): product contract",
        {f"contracts/{_TICKET}.yaml": contract(_TICKET, "dod-a", [])},
    )
    world.install(monkeypatch)

    results = EvidenceCollector().collect(_TICKET)

    assert len(results) == 1
    assert results[0].evidence_id == "dod-a", results[0].message
    assert results[0].status == EnumEvidenceCheckStatus.VERIFIED, results[0].message


@pytest.mark.unit
def test_evidence_collector_prefers_product_contract_over_stale_occ_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path)
    world.occ({_TICKET: contract(_TICKET, "dod-STALE", [])})
    world.merge(
        "omnimarket",
        12,
        f"feat({_TICKET}): current product contract",
        {f"contracts/{_TICKET}.yaml": contract(_TICKET, "dod-FRESH", [])},
    )
    world.install(monkeypatch)

    results = EvidenceCollector().collect(_TICKET)

    assert len(results) == 1
    assert results[0].evidence_id == "dod-FRESH", results[0].message
