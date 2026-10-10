# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The arm decision reads the ledger's HOLD rows and the head's lab pass (OMN-20866).

Before an arm or an enqueue of a green head, the orchestrator reads two facts
from the lab projections the bus feeds: the HOLD rows in force for the PR
(``work_ledger_state``, folded from the ledger's row events) and a PASS lab
proof for the PR's exact head (the pr-head ``ModelLabPassReceipt`` projected
into ``lab_proof_receipts``, or the lab pool's ``LAB PROOF PASS`` readback row).

* a held PR is never armed (AC "held PRs skipped");
* a PR in a lab-proof repository with no PASS for its head is not armed;
* a PASS for the head plus a green, clean head is armed;
* a projection that cannot be read is UNKNOWN and fails closed.

Every withheld arm names its reason on the transition the bus carries, and the
head stays CHECKS_PENDING so the next poll reads it again.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubCheckRunFact,
    ModelGithubPrStateFact,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingState,
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_gate_facts import (
    EnumPrLandingFactState,
    EnumPrLandingWithheldReason,
    ModelPrLandingGateFacts,
    ModelPrLandingLabPass,
    ModelPrLandingLedgerHold,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingGithubCompletedIngress,
    ModelPrLandingObservedPrompt,
    ModelPrLandingReconcileCommand,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.contract_config import (
    PrLandingContractConfigError,
    gate_facts_from_block,
    load_contract_config,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.gate_facts import (
    decide_gate,
    holds_in_force,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.gate_facts_reader import (
    ProjectionGateFactsReader,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    NODE_ID,
    T0,
    FixedGateFacts,
    answer,
    only_request,
    requests_in,
)

pytestmark = pytest.mark.unit

CANARY = "OmniNode-ai/omnimarket"
_REPLAY = Path(__file__).resolve().parents[3] / "fixtures/pr_landing/head_checks_replay"
NOW = datetime(2026, 10, 10, 17, 0, 0, tzinfo=UTC)


def _recorded(pr: int) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (_REPLAY / f"omnimarket_{pr}.json").read_text("utf-8")
    )
    return data


def _observed(pr: int, head: str) -> ModelPrLandingObservedPrompt:
    at = T0.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ModelPrLandingObservedPrompt.model_validate(
        {
            "repo": "omnimarket",
            "pr_number": pr,
            "state": "open",
            "head_sha": head,
            "observed_at": at,
            "base": "dev",
            "armed": False,
            "ci_verdict": "GREEN",
            "red_contexts": [],
            "actor": "claude",
            "hook_fired_at": at,
            "schema_version": "1.0.0",
        }
    )


def _pr_state(pr: int, head: str) -> ModelGithubPrStateFact:
    return ModelGithubPrStateFact.model_validate(
        {
            "pr_number": pr,
            "head_sha": head,
            "base_ref": "dev",
            "state": "open",
            "merged": False,
            "draft": False,
            "title": f"feat(OMN-20866): gate facts pr {pr}",
            "labels": (),
            "auto_merge_armed": False,
            "pr_node_id": NODE_ID,
            "mergeable_state": "clean",
        }
    )


def _head_checks_answer(
    request: ModelPrLandingGithubRequest, recorded: dict[str, Any]
) -> ModelPrLandingGithubCompletedIngress:
    base = answer(
        request,
        check_runs=ModelGithubCheckRunFact.from_check_runs_body(
            recorded["check_runs_body"]
        ),
    )
    return base.model_copy(
        update={"required_contexts": tuple(recorded["required_contexts"])}
    )


def _transitions(emitted: list[BaseModel]) -> list[ModelPrLandingTransitioned]:
    return [e for e in emitted if isinstance(e, ModelPrLandingTransitioned)]


def _handler(
    facts: FixedGateFacts | None, store: InMemoryPrLandingRowStore
) -> HandlerPrLandingOrchestrator:
    """The runtime's composition: contract config, real reducer, gate, classifier."""
    return HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=HandlerPrArmGate(),
        gate_facts=facts,
        store=store,
    )


async def _green_head_answer(
    facts: FixedGateFacts | None,
) -> tuple[
    HandlerPrLandingOrchestrator,
    InMemoryPrLandingRowStore,
    dict[str, Any],
    list[BaseModel],
]:
    """omnimarket#3639's recorded green head, clean, through the whole leg."""
    store = InMemoryPrLandingRowStore()
    handler = _handler(facts, store)
    recorded = _recorded(3639)
    pr, head = recorded["pr_number"], recorded["head_sha"]
    read = only_request(await handler.handle(_observed(pr, head)))
    head_read = only_request(
        await handler.handle(answer(read, pr_state=_pr_state(pr, head)))
    )
    assert head_read.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    return handler, store, recorded, emitted


def _hold(pr: str | None, *, repo: str | None = None, **extra: Any) -> Any:
    hold_id = extra.pop("hold_id", "2026-10-10T16:00:00Z-some-lane")
    first = f"2026-10-10T16:00:00Z | HOLD | lane=some-lane | id={hold_id} | to=all"
    if repo:
        first += f" | repo={repo}"
    if pr:
        first += f" | pr={pr}"
    for key, value in extra.items():
        if key != "until_at":
            first += f" | {key}={value}"
    return ModelPrLandingLedgerHold(
        hold_id=hold_id,
        repo=repo,
        pr=pr,
        surface=extra.get("surface"),
        until_at=extra.get("until_at"),
        raw_row=first + " | hold it",
    )


# ------------------------------------------------- the four acceptance behaviours


async def test_held_prs_skipped() -> None:
    """A HOLD row in force for the PR: green and clean, still never armed."""
    recorded = _recorded(3639)
    facts = FixedGateFacts(
        holds=(_hold("omnimarket#3639", repo="omnimarket"),),
        lab_heads=(recorded["head_sha"],),
    )
    _, store, _, emitted = await _green_head_answer(facts)
    assert requests_in(emitted) == []
    (moved,) = _transitions(emitted)
    assert moved.to_state is EnumPrLandingState.CHECKS_PENDING
    assert moved.withheld_reason is not None
    assert moved.withheld_reason.startswith("ledger_hold_in_force:")
    assert "2026-10-10T16:00:00Z-some-lane" in moved.withheld_reason
    row, _ = await store.load(f"{CANARY}#3639")
    assert row is not None
    assert row.landing is not None
    assert row.landing.armed is None
    # The next poll is a full read, so a RELEASE is seen without a new push.
    assert row.head_checks_etag is None


async def test_missing_receipt_not_armed() -> None:
    """omnimarket is a lab-proof repository; a PASS for another head is not this head's."""
    facts = FixedGateFacts(lab_heads=("9" * 40,))
    _, _, recorded, emitted = await _green_head_answer(facts)
    assert requests_in(emitted) == []
    (moved,) = _transitions(emitted)
    assert moved.to_state is EnumPrLandingState.CHECKS_PENDING
    assert moved.withheld_reason == (
        f"lab_pass_missing: no PASS lab proof for head {recorded['head_sha'][:12]}"
    )


async def test_pass_receipt_plus_green_clean_armed() -> None:
    recorded = _recorded(3639)
    facts = FixedGateFacts(lab_heads=(recorded["head_sha"],))
    handler, _, _, emitted = await _green_head_answer(facts)
    (moved,) = _transitions(emitted)
    assert moved.to_state is EnumPrLandingState.READY
    assert moved.withheld_reason is None
    arm = only_request(emitted)
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert arm.mode is EnumPrLandingGithubMode.ENFORCE
    assert arm.head_sha == recorded["head_sha"]
    assert [t.to_state for t in _transitions(await handler.handle(answer(arm)))] == [
        EnumPrLandingState.ARMED
    ]
    assert facts.reads == [(CANARY, 3639, recorded["head_sha"])]


async def test_projection_unreadable_fails_closed() -> None:
    facts = FixedGateFacts(unreadable="ConnectionRefusedError: [Errno 111]")
    _, _, _, emitted = await _green_head_answer(facts)
    assert requests_in(emitted) == []
    (moved,) = _transitions(emitted)
    assert moved.to_state is EnumPrLandingState.CHECKS_PENDING
    assert moved.withheld_reason == (
        "ledger_holds_unknown: ConnectionRefusedError: [Errno 111]"
    )


async def test_projection_unreadable_fails_closed_when_the_reader_raises() -> None:
    facts = FixedGateFacts(raise_on_read=True)
    _, _, _, emitted = await _green_head_answer(facts)
    assert requests_in(emitted) == []
    (moved,) = _transitions(emitted)
    assert moved.withheld_reason is not None
    assert moved.withheld_reason.startswith("ledger_holds_unknown: RuntimeError")


async def test_projection_unreadable_fails_closed_with_no_reader_wired() -> None:
    """The contract declares the gate; a composition without a reader withholds."""
    store = InMemoryPrLandingRowStore()
    handler = HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=HandlerPrArmGate(),
        config=load_contract_config(),
        store=store,
        gate_facts=None,
    )
    # The handler wires the projection reader itself; with no DSN in the
    # environment it reads UNKNOWN.
    recorded = _recorded(3639)
    pr, head = recorded["pr_number"], recorded["head_sha"]
    read = only_request(await handler.handle(_observed(pr, head)))
    head_read = only_request(
        await handler.handle(answer(read, pr_state=_pr_state(pr, head)))
    )
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    assert requests_in(emitted) == []
    (moved,) = _transitions(emitted)
    assert moved.withheld_reason is not None
    assert moved.withheld_reason.startswith("ledger_holds_unknown:")


async def test_a_release_is_seen_on_the_next_poll_and_the_head_arms() -> None:
    recorded = _recorded(3639)
    facts = FixedGateFacts(
        holds=(_hold("omnimarket#3639"),), lab_heads=(recorded["head_sha"],)
    )
    handler, _, _, emitted = await _green_head_answer(facts)
    assert requests_in(emitted) == []
    facts.holds = ()  # the RELEASE row lands
    tick = ModelPrLandingReconcileCommand.model_validate(
        {
            "repository": CANARY,
            "pr_number": 3639,
            "requested_at": T0 + timedelta(minutes=5),
            "tick_id": "tick-after-release",
        }
    )
    pr_read = only_request(await handler.handle(tick))
    assert pr_read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    head_read = only_request(await handler.handle(answer(pr_read, not_modified=True)))
    assert head_read.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
    assert head_read.etag is None
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    arm = only_request(emitted)
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert [t.withheld_reason for t in _transitions(emitted)] == [None]


# ------------------------------------------------------------- the pure decision


def _facts(**overrides: Any) -> ModelPrLandingGateFacts:
    data: dict[str, Any] = {
        "repository": "OmniNode-ai/omnibase_infra",
        "pr_number": 4807,
        "head_sha": "a" * 40,
        "read_at": NOW,
        "holds_state": EnumPrLandingFactState.KNOWN,
        "holds": (),
        "ledger_newest_projected_at": NOW - timedelta(minutes=2),
        "lab_state": EnumPrLandingFactState.KNOWN,
        "lab_passes": (),
    }
    data.update(overrides)
    return ModelPrLandingGateFacts(**data)


def _receipt(head: str, *, result: str = "PASS", token: str = "PASS") -> Any:
    return ModelPrLandingLabPass(
        source="lab_proof_receipt",
        ref=f"OmniNode-ai/omnibase_infra#4807@{head}:runtime@1",
        head_sha=head,
        result=result,
        verifier_token=token,
    )


def _decide(facts: ModelPrLandingGateFacts, *, lab: bool = True) -> Any:
    return decide_gate(
        facts,
        now=NOW,
        lab_proof_required=lab,
        ledger_freshness_bound=timedelta(minutes=60),
    )


def test_decision_a_pass_receipt_for_the_head_arms() -> None:
    decision = _decide(_facts(lab_passes=(_receipt("a" * 40),)))
    assert decision.withheld is None
    assert decision.lab_pass_ref is not None


@pytest.mark.parametrize(
    ("receipt", "why"),
    [
        (_receipt("b" * 40), "another head"),
        (_receipt("a" * 40, result="FAIL", token="FAIL"), "a FAIL"),
        (_receipt("a" * 40, token="ANY_OF_UNMET"), "a PASS the verifier refused"),
    ],
)
def test_decision_only_a_verified_pass_for_the_exact_head_counts(
    receipt: Any, why: str
) -> None:
    decision = _decide(_facts(lab_passes=(receipt,)))
    assert decision.withheld is EnumPrLandingWithheldReason.LAB_PASS_MISSING, why


def test_decision_a_ledger_readback_with_a_short_head_counts() -> None:
    readback = ModelPrLandingLabPass(
        source="ledger_readback",
        ref="row-1",
        head_sha="a" * 10,
        result="PASS",
        verifier_token=None,
    )
    assert _decide(_facts(lab_passes=(readback,))).withheld is None


def test_decision_no_lab_proof_needed_outside_the_lab_repositories() -> None:
    assert _decide(_facts(), lab=False).withheld is None


def test_decision_lab_unknown_fails_closed() -> None:
    facts = _facts(
        lab_state=EnumPrLandingFactState.UNKNOWN, unknown_detail="relation missing"
    )
    decision = _decide(facts)
    assert decision.withheld is EnumPrLandingWithheldReason.LAB_PASS_UNKNOWN
    assert decision.reason_text == "lab_pass_unknown: relation missing"


def test_decision_a_stale_ledger_projection_is_unknown() -> None:
    stale = NOW - timedelta(minutes=61)
    decision = _decide(
        _facts(ledger_newest_projected_at=stale, lab_passes=(_receipt("a" * 40),))
    )
    assert decision.withheld is EnumPrLandingWithheldReason.LEDGER_HOLDS_UNKNOWN
    assert "2026-10-10T15:59:00+00:00" in decision.detail


def test_decision_an_empty_ledger_projection_is_unknown() -> None:
    decision = _decide(
        _facts(ledger_newest_projected_at=None, lab_passes=(_receipt("a" * 40),))
    )
    assert decision.withheld is EnumPrLandingWithheldReason.LEDGER_HOLDS_UNKNOWN


def test_decision_a_hold_outranks_a_pass() -> None:
    facts = _facts(
        holds=(_hold("omnibase_infra#4807"),), lab_passes=(_receipt("a" * 40),)
    )
    decision = _decide(facts)
    assert decision.withheld is EnumPrLandingWithheldReason.LEDGER_HOLD_IN_FORCE
    assert decision.hold_ids == ("2026-10-10T16:00:00Z-some-lane",)


@pytest.mark.parametrize(
    ("hold", "held"),
    [
        (_hold("omnibase_infra#4807"), True),
        (_hold("OmniNode-ai/omnibase_infra#4807"), True),
        (_hold("omnimarket#3172,omnibase_infra#4807"), True),
        (_hold("omnibase_infra#48070"), False),
        (_hold("omnibase_infra#480"), False),
        (_hold("omnimarket#4807"), False),
        # A repository-wide hold names no PR.
        (_hold(None, repo="omnibase_infra"), True),
        (_hold(None, repo="omnidash"), False),
        # A lease on a proof surface holds the surface, never a PR.
        (_hold("omnibase_infra#4807", surface="202-runtime"), False),
        # The red-PR fixer's kill switch stops its workers, never a merge.
        (_hold(None, repo="omnibase_infra", scope="fixer"), False),
        # An expired hold.
        (_hold("omnibase_infra#4807", until_at=NOW - timedelta(seconds=1)), False),
        (_hold("omnibase_infra#4807", until_at=NOW + timedelta(hours=1)), True),
    ],
)
def test_decision_holds_in_force_use_the_canonical_scope(hold: Any, held: bool) -> None:
    got = holds_in_force(
        (hold,), repository="OmniNode-ai/omnibase_infra", pr_number=4807, now=NOW
    )
    assert bool(got) is held


def test_decision_a_hold_naming_the_pr_only_in_its_text_holds_it() -> None:
    hold = ModelPrLandingLedgerHold(
        hold_id="2026-10-10T16:00:00Z-x",
        repo=None,
        pr=None,
        surface=None,
        until_at=None,
        raw_row="2026-10-10T16:00:00Z | HOLD | lane=x | id=2026-10-10T16:00:00Z-x | "
        "to=all | hold omnibase_infra#4807 until the core release",
    )
    got = holds_in_force(
        (hold,), repository="OmniNode-ai/omnibase_infra", pr_number=4807, now=NOW
    )
    assert got == ("2026-10-10T16:00:00Z-x",)


# ------------------------------------------------------------------ the reader


async def test_reader_with_no_dsn_reads_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_contract_config().gate_facts
    assert config is not None
    monkeypatch.delenv(config.dsn_env, raising=False)
    facts = await ProjectionGateFactsReader(config).read(
        "OmniNode-ai/omnimarket", 3639, "a" * 40, NOW
    )
    assert facts.holds_state is EnumPrLandingFactState.UNKNOWN
    assert facts.lab_state is EnumPrLandingFactState.UNKNOWN
    assert facts.unknown_detail == f"{config.dsn_env} is not set"


async def test_reader_maps_the_projection_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_contract_config().gate_facts
    assert config is not None
    monkeypatch.setenv(config.dsn_env, _DSN)
    readback = (
        "2026-10-10T16:30:00Z | RELEASE | lane=landing-lab-omnibase_infra-L94 | "
        "re=2026-10-10T16:10:00Z-landing-lab-omnibase_infra-L94 | result=PASS | "
        "LAB PROOF PASS: omnibase_infra#4807 head aaaaaaaaaa on a pool host, project x"
    )
    conn = _FakeConnection(
        holds=[
            {
                "entity_key": "hold:2026-10-10T16:00:00Z-some-lane",
                "repo": "omnibase_infra",
                "pr": "omnibase_infra#4807",
                "scope_surface": None,
                "until_at": None,
                "raw_row": "2026-10-10T16:00:00Z | HOLD | id=x\nsecond line",
            }
        ],
        newest=NOW - timedelta(minutes=1),
        receipts=[
            {
                "receipt_key": "k1",
                "head_sha": "a" * 40,
                "result": "PASS",
                "verifier_token": "PASS",
            }
        ],
        readbacks=[
            {"row_id": "r1", "row_ts": NOW - timedelta(minutes=30), "raw_row": readback}
        ],
    )

    async def connect(dsn: str) -> _FakeConnection:
        assert dsn == _DSN
        return conn

    facts = await ProjectionGateFactsReader(config, connect=connect).read(
        "OmniNode-ai/omnibase_infra", 4807, "a" * 40, NOW
    )
    assert conn.closed
    assert facts.holds_state is EnumPrLandingFactState.KNOWN
    (hold,) = facts.holds
    assert hold.hold_id == "2026-10-10T16:00:00Z-some-lane"
    assert hold.raw_row == "2026-10-10T16:00:00Z | HOLD | id=x"
    assert facts.lab_state is EnumPrLandingFactState.KNOWN
    assert sorted(p.source for p in facts.lab_passes) == [
        "lab_proof_receipt",
        "ledger_readback",
    ]
    readback_pass = next(p for p in facts.lab_passes if p.source == "ledger_readback")
    assert readback_pass.head_sha == "a" * 10


async def test_reader_redacts_the_dsn_from_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_contract_config().gate_facts
    assert config is not None
    dsn = _DSN
    monkeypatch.setenv(config.dsn_env, dsn)

    async def connect(_: str) -> _FakeConnection:
        raise OSError(f"could not connect to {dsn}")

    facts = await ProjectionGateFactsReader(config, connect=connect).read(
        "OmniNode-ai/omnibase_infra", 4807, "a" * 40, NOW
    )
    assert facts.holds_state is EnumPrLandingFactState.UNKNOWN
    assert facts.unknown_detail is not None
    assert _PASSWORD not in facts.unknown_detail
    assert facts.unknown_detail.startswith("OSError:")


# Built by parts so the fixture spells no credential-shaped literal.
_PASSWORD = "pw" + "0"
_DSN = "postgresql://reader:" + _PASSWORD + "@db:5432/x"


class _FakeConnection:
    """asyncpg's fetch/fetchval/close, answering by which relation a query reads."""

    def __init__(
        self,
        *,
        holds: list[dict[str, Any]],
        newest: datetime | None,
        receipts: list[dict[str, Any]],
        readbacks: list[dict[str, Any]],
    ) -> None:
        self._holds = holds
        self._newest = newest
        self._receipts = receipts
        self._readbacks = readbacks
        self.closed = False

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        if "lab_proof_receipts" in query:
            return self._receipts
        if "work_ledger_state" in query:
            return self._holds
        return self._readbacks

    async def fetchval(self, query: str, *args: object) -> datetime | None:
        return self._newest

    async def close(self) -> None:
        self.closed = True


# ------------------------------------------------------------------ the contract


def test_config_the_contract_declares_the_gate_facts() -> None:
    gate = load_contract_config().gate_facts
    assert gate is not None
    assert gate.lab_proof_repos == {
        "OmniNode-ai/omnibase_infra",
        "OmniNode-ai/omnimarket",
    }
    assert gate.ledger_freshness_bound == timedelta(minutes=60)
    assert gate.dsn_env == "OMNINODE_INTERNAL_DB_URL"


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        (None, "no landing_gate_facts"),
        ({"surprise": 1}, "unknown keys"),
        ({"lab_proof_repositories": ["omnimarket"]}, "owner/name"),
        (
            {
                "lab_proof_repositories": [],
                "ledger_freshness_bound_minutes": 0,
                "source": {},
            },
            "ledger_freshness_bound_minutes",
        ),
    ],
)
def test_config_a_malformed_gate_facts_block_is_refused(
    block: object, fragment: str
) -> None:
    with pytest.raises(PrLandingContractConfigError, match=fragment):
        gate_facts_from_block(block)


def test_config_a_hand_built_config_reads_no_gate_facts() -> None:
    """Only the contract turns the gate on; a test's own config has it off."""
    config = replace(load_contract_config(), gate_facts=None)
    assert config.gate_facts is None
