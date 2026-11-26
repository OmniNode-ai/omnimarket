# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-25366 — node_dod_verify resolves a ticket's contract from the repository
of the ticket's merged PR.

The closeout of a sprint's kept tickets read every one as CONTRACT_MISSING:
the node counted a product-repository contract only when the deciding PR's head
also ran ``repo-evidence / dod-verify``, and only in repositories whose default
branch already carried the contract. A ticket whose merged PR predates that
workflow, or whose contract landed in a later PR, was invisible.

The world is the one the repo-first suite uses: real git clones with squash
commits and a PR watcher state; every GitHub read but the check runs fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.enums.enum_dod_contract_source import EnumDodContractSource
from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import (
    World,
    contract,
)

pytestmark = pytest.mark.unit

_AUDIENCE = EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_contract_at_merge_commit_resolves_without_a_repo_evidence_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merged PR carries the contract but its head ran no repo-evidence
    check: the contract still decides what the closeout verifies."""
    ticket = "OMN-90301"
    merged = world.merge(
        "omnimarket",
        11,
        f"feat({ticket}): the work",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-a", ["AC1"])},
        run=None,
    )
    world.install(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(ticket, execution_audience=_AUDIENCE)

    assert [r.evidence_id for r in results] == ["dod-a"]
    assert results[0].status is EnumEvidenceCheckStatus.VERIFIED
    subject = collector.contract_subject
    assert subject is not None
    assert subject.source is EnumDodContractSource.PRODUCT_REPOSITORY
    assert subject.repository == "OmniNode-ai/omnimarket"
    assert subject.commit_sha == merged["merge_sha"]
    # The durable gate never resolved this repository, so it is not run on it.
    assert collector._durable_gate_inputs is None


def test_contract_only_on_default_branch_resolves_from_the_merged_prs_repo(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket's PR merged before the contract existed; a later PR (which
    names no ticket) added it to the same repository's default branch."""
    ticket = "OMN-90302"
    world.merge("omnibase_core", 21, f"fix({ticket}): the work", {"src/x.py": "x\n"})
    later = world.merge(
        "omnibase_core",
        22,
        "chore: evidence bindings",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-late", ["AC1"])},
    )
    world.install(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(ticket, execution_audience=_AUDIENCE)

    assert [r.evidence_id for r in results] == ["dod-late"]
    subject = collector.contract_subject
    assert subject is not None
    assert subject.repository == "OmniNode-ai/omnibase_core"
    assert subject.commit_sha == later["merge_sha"]


def test_only_repositories_with_a_merged_pr_for_the_ticket_supply_a_contract(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two repositories merged PRs for the ticket; only one holds the contract.
    That repository's contract runs and the other is not read as governing."""
    ticket = "OMN-90303"
    world.merge("omnibase_infra", 31, f"fix({ticket}): infra side", {"src/x.py": "x\n"})
    world.merge(
        "omniclaude",
        32,
        f"feat({ticket}): client side",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-client", ["AC1"])},
        run=None,
    )
    world.install(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(ticket, execution_audience=_AUDIENCE)

    assert [r.evidence_id for r in results] == ["dod-client"]
    assert collector.contract_subject is not None
    assert collector.contract_subject.repository == "OmniNode-ai/omniclaude"


def test_no_merged_pr_stays_contract_missing_and_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90304"
    world.merge("omnimarket", 41, "feat(OMN-90399): another ticket", {"a.py": "a\n"})
    world.install(monkeypatch)

    results = EvidenceCollector().collect(ticket, execution_audience=_AUDIENCE)

    assert len(results) == 1
    assert results[0].status is EnumEvidenceCheckStatus.FAILED
    message = results[0].message or ""
    assert message.startswith("CONTRACT_MISSING:")
    assert f"no merged pull request names {ticket}" in message
    assert "holds no contracts/" not in message


def test_merged_pr_in_a_repo_with_no_contract_stays_contract_missing_and_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90305"
    world.merge("omnibase_core", 51, f"fix({ticket}): the work", {"src/x.py": "x\n"})
    world.install(monkeypatch)

    results = EvidenceCollector().collect(ticket, execution_audience=_AUDIENCE)

    assert len(results) == 1
    assert results[0].status is EnumEvidenceCheckStatus.FAILED
    message = results[0].message or ""
    assert message.startswith("CONTRACT_MISSING:")
    assert f"holds no contracts/{ticket}.yaml" in message
    assert "OmniNode-ai/omnibase_core#51" in message
    assert "no merged pull request names" not in message


def test_malformed_contract_in_the_merged_prs_repo_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90306"
    world.merge(
        "omnimarket",
        61,
        f"feat({ticket}): broken",
        {f"contracts/{ticket}.yaml": "dod_evidence: [unclosed\n"},
        run=None,
    )
    world.install(monkeypatch)

    results = EvidenceCollector().collect(ticket, execution_audience=_AUDIENCE)

    assert len(results) == 1
    assert results[0].status is EnumEvidenceCheckStatus.FAILED
    assert (results[0].message or "").startswith("REPO_CONTRACT_UNRESOLVED:")
