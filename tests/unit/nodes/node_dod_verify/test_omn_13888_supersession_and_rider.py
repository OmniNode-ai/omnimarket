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
    EnumDurableEvidenceStatus,
    EnumRepoContractReadStatus,
    ModelRepoContractRead,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    DurableEvidenceGate,
    apply_supersessions,
    extract_receipt_merge_commits,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import World, contract

_TICKET = "OMN-13899"
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


@pytest.mark.unit
def test_a_supersession_rebind_no_longer_admits_a_ticket_through_the_gate() -> None:
    """The gate reads no receipt trail (OMN-20074): a rebound OCC receipt is refused."""
    gate = DurableEvidenceGate(
        release_tags_containing=lambda _repo, _sha: ("v9.9.9",),
        index_release_files=lambda _d, _v: frozenset({"bdist_wheel", "sdist"}),
        read_repo_contract=lambda _repo, _ref, _ticket: ModelRepoContractRead(
            status=EnumRepoContractReadStatus.ABSENT
        ),
        read_repo_check_runs=lambda _repo, _sha: (),
    )
    result = gate.evaluate(
        ticket_id=_TICKET,
        contract={"ticket_id": _TICKET, "dod_evidence": []},
        ticket_labels=frozenset(),
    )
    assert result.status is EnumDurableEvidenceStatus.FAIL
    governing = next(
        c
        for c in result.checks
        if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
    )
    assert governing.passed is False


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
