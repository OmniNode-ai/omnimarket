# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20886 — node_dod_verify resolves a ticket's contract from the product
repository first, and runs the DurableEvidenceGate on that path.

With no contract path the node used to search onex_change_control only, so a
ticket whose contract is ``contracts/<TICKET>.yaml`` in a cut-over product
repository could not be verified without a path passed by hand; and the
DurableEvidenceGate was constructed only in tests, so its pre-Done checks never
ran when the node did.

The world here is a registry root of real git clones (``origin/dev`` carrying
squash commits whose subjects end ``(#<n>)``) plus a PR watcher state holding
each merged PR's head, merge commit and merge time: the node's local read
source, used as production uses it. ``gh`` is patched: every read but the
repo-evidence check runs fails the test, and the check runs come from a table.

The onex_change_control-only cases replay the verdicts recorded by running the
same worlds at the base commit (``fixtures/omn20886_occ_only_lookup_replay.json``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.enums.enum_dod_contract_source import EnumDodContractSource
from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_durable_evidence_gate_effect import (
    HandlerDurableEvidenceGateEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import (
    World,
    contract,
    occ_only_world,
    snapshot,
)

pytestmark = pytest.mark.unit

_REPLAY = Path(__file__).parent / "fixtures" / "omn20886_occ_only_lookup_replay.json"
_REAL_RUN_GATE = EvidenceCollector.run_durable_gate
_AUDIENCE = EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# --------------------------------------------------------------------------- #
# AC1: one product repository.
# --------------------------------------------------------------------------- #


def test_single_repo_contract_resolves_at_newest_merged_pr_merge_commit(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two merged PRs carry the contract; the newer one's merge commit governs,
    and no onex_change_control clone is consulted (none exists here)."""
    ticket = "OMN-90201"
    world.merge(
        "omnimarket",
        11,
        f"feat({ticket}): first",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-first", ["AC1"])},
    )
    newest = world.merge(
        "omnimarket",
        12,
        f"fix({ticket}): second",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-second", ["AC1"])},
    )
    world.install(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(ticket, execution_audience=_AUDIENCE)

    assert [r.evidence_id for r in results] == ["dod-second"]
    subject = collector.contract_subject
    assert subject is not None
    assert subject.source is EnumDodContractSource.PRODUCT_REPOSITORY
    assert subject.repository == "OmniNode-ai/omnimarket"
    assert subject.commit_sha == newest["merge_sha"]
    assert subject.repo_path == f"contracts/{ticket}.yaml"


def test_single_repo_malformed_contract_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A product contract that does not parse refuses; no OCC copy stands in."""
    ticket = "OMN-90205"
    world.merge(
        "omnimarket",
        21,
        f"feat({ticket}): broken",
        {f"contracts/{ticket}.yaml": "dod_evidence: [unclosed\n"},
    )
    world.install(monkeypatch)

    results = EvidenceCollector().collect(ticket, execution_audience=_AUDIENCE)

    assert len(results) == 1
    assert results[0].status is EnumEvidenceCheckStatus.FAILED
    assert (results[0].message or "").startswith("REPO_CONTRACT_UNRESOLVED:")


# --------------------------------------------------------------------------- #
# AC2: two product repositories.
# --------------------------------------------------------------------------- #


def test_two_repos_resolve_per_repository_and_every_contract_runs(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each repository's newest contract-carrying PR decides that repository;
    both contracts' evidence runs, kept apart by repository."""
    ticket = "OMN-90202"
    world.merge(
        "omnimarket",
        31,
        f"feat({ticket}): market old",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-market-old", ["AC1"])},
    )
    world.merge(
        "omnimarket",
        32,
        f"feat({ticket}): market new",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-tests", ["AC1"])},
    )
    world.merge(
        "omnibase_infra",
        41,
        f"feat({ticket}): infra old",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-infra-old", ["AC2"])},
    )
    world.merge(
        "omnibase_infra",
        42,
        f"feat({ticket}): infra new",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-tests", ["AC2"])},
    )
    world.install(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(ticket, execution_audience=_AUDIENCE)

    assert sorted(r.evidence_id for r in results) == [
        "omnibase_infra:dod-tests",
        "omnimarket:dod-tests",
    ]
    subject = collector.contract_subject
    assert subject is not None
    assert subject.source is EnumDodContractSource.UNBOUND
    assert subject.commit_sha is None


def test_two_repos_one_without_repo_evidence_run_is_not_governing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository whose deciding PR has no repo-evidence run has not adopted
    the repo path (the gate's resolution); the other repository governs."""
    ticket = "OMN-90206"
    world.merge(
        "omnimarket",
        51,
        f"feat({ticket}): market",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-market", ["AC1"])},
    )
    world.merge(
        "omnibase_infra",
        61,
        f"feat({ticket}): infra",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-infra", ["AC1"])},
        run=None,
    )
    world.install(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(ticket, execution_audience=_AUDIENCE)

    assert [r.evidence_id for r in results] == ["dod-market"]
    assert collector.contract_subject is not None
    assert collector.contract_subject.repository == "OmniNode-ai/omnimarket"


# --------------------------------------------------------------------------- #
# AC3: onex_change_control only, and no contract anywhere.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "case",
    ["product_prs_without_contract", "product_contract_without_repo_evidence_run"],
)
def test_occ_only_ticket_resolves_exactly_as_recorded(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    ticket = occ_only_world(world, case)
    world.install(monkeypatch)

    collector = EvidenceCollector()
    try:
        results = collector.collect(ticket, execution_audience=_AUDIENCE)
    finally:
        collector.release_occ_dev_snapshot()

    recorded = json.loads(_REPLAY.read_text(encoding="utf-8"))["cases"][case]
    assert snapshot(collector, results, world) == recorded


def test_no_contract_anywhere_still_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90204"
    world.merge("omnimarket", 81, f"feat({ticket}): no contract", {"src/x.py": "x\n"})
    world.occ({})
    world.install(monkeypatch)

    collector = EvidenceCollector()
    monkeypatch.setattr(
        HandlerDodVerify, "_make_collector", staticmethod(lambda: collector)
    )
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(ticket_id=ticket, execution_audience=_AUDIENCE)
    )
    collector.release_occ_dev_snapshot()

    assert isinstance(state, ModelDodVerifyState)
    assert state.status is not EnumDodVerifyStatus.VERIFIED
    assert [c.evidence_id for c in state.checks] == ["contract"]
    assert collector.run_durable_gate is not None
    assert collector._durable_gate_inputs is None


# --------------------------------------------------------------------------- #
# AC4: the DurableEvidenceGate runs on the node's path.
# --------------------------------------------------------------------------- #

_DESCRIPTION_AC1 = (
    "## Acceptance criteria\n\n- [ ] AC1: it works -- falsifier: uv run pytest t.py\n"
)
_DESCRIPTION_AC1_AC2 = (
    _DESCRIPTION_AC1 + "- [ ] AC2: it also works -- falsifier: uv run pytest u.py\n"
)


def _verify_with_gate(
    world: World, monkeypatch: pytest.MonkeyPatch, ticket: str, description: str
) -> ModelDodVerifyState:
    world.install(monkeypatch)
    monkeypatch.setattr(EvidenceCollector, "run_durable_gate", _REAL_RUN_GATE)
    monkeypatch.setattr(
        HandlerDurableEvidenceGateEffect,
        "read_ticket",
        staticmethod(lambda _ticket: (description, frozenset(), "")),
    )
    collector = EvidenceCollector()
    monkeypatch.setattr(
        HandlerDodVerify, "_make_collector", staticmethod(lambda: collector)
    )
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(ticket_id=ticket, execution_audience=_AUDIENCE)
    )
    assert isinstance(state, ModelDodVerifyState)
    return state


def _gate_checks(state: ModelDodVerifyState) -> dict[str, ModelEvidenceCheckResult]:
    return {
        c.evidence_id: c
        for c in state.checks
        if c.evidence_id.startswith("durable_gate::")
    }


def test_gate_runs_on_repo_path_and_its_three_checks_are_verdict_checks(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90207"
    world.merge(
        "omnimarket",
        91,
        f"feat({ticket}): bound",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-bound", ["AC1"])},
    )

    state = _verify_with_gate(world, monkeypatch, ticket, _DESCRIPTION_AC1)

    gate = _gate_checks(state)
    assert sorted(gate) == [
        "durable_gate::contract_cites_merge_commit",
        "durable_gate::contract_on_occ_main",
        "durable_gate::receipt_tracked",
    ]
    assert all(c.status is EnumEvidenceCheckStatus.VERIFIED for c in gate.values())
    assert "Product-repository contract governs" in (
        gate["durable_gate::contract_on_occ_main"].message or ""
    )


def test_gate_refuses_an_unbound_criterion(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90208"
    world.merge(
        "omnimarket",
        101,
        f"feat({ticket}): binds AC1 only",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-bound", ["AC1"])},
    )

    state = _verify_with_gate(world, monkeypatch, ticket, _DESCRIPTION_AC1_AC2)

    on_main = _gate_checks(state)["durable_gate::contract_on_occ_main"]
    assert on_main.status is EnumEvidenceCheckStatus.FAILED
    assert "AC2" in (on_main.message or "")
    assert state.status is EnumDodVerifyStatus.FAILED


def test_gate_refuses_red_repo_evidence(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red run on the deciding head engages the repo path and refuses it."""
    ticket = "OMN-90209"
    world.merge(
        "omnimarket",
        111,
        f"feat({ticket}): red",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-bound", ["AC1"])},
        run="failure",
    )

    state = _verify_with_gate(world, monkeypatch, ticket, _DESCRIPTION_AC1)

    on_main = _gate_checks(state)["durable_gate::contract_on_occ_main"]
    assert on_main.status is EnumEvidenceCheckStatus.FAILED
    assert "conclusion=failure" in (on_main.message or "")
    assert state.status is EnumDodVerifyStatus.FAILED


def test_gate_refuses_when_repo_evidence_is_unreadable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable check runs refuse at resolution; OCC is not consulted."""
    ticket = "OMN-90210"
    pr = world.merge(
        "omnimarket",
        121,
        f"feat({ticket}): unreadable",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-bound", ["AC1"])},
    )
    world.check_runs[("OmniNode-ai/omnimarket", pr["head_sha"])] = [
        {"unreadable": True}
    ]
    world.occ({ticket: contract(ticket, "dod-occ", ["AC1"])})

    state = _verify_with_gate(world, monkeypatch, ticket, _DESCRIPTION_AC1)

    assert [c.evidence_id for c in state.checks] == ["contract"]
    assert (state.checks[0].message or "").startswith("REPO_CONTRACT_UNRESOLVED:")
    assert state.status is EnumDodVerifyStatus.FAILED
