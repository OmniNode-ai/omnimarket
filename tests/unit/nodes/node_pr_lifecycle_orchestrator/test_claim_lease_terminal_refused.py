# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC-M6: a claim whose TERMINAL is refused stops owning its PR at the lease TTL.

On 2026-10-10 lane TERMINAL rows were refused (no signing principal) and the
CLAIMs they would have closed stayed open, holding PRs away from the
controller. The owner claim the bus path writes is a lease: the reducer stamps
its expiry (``next_check_at``) from the TTL its contract declares, the owner
read counts a claim only before that expiry, and the ledger host emits a typed
TERMINAL_REFUSED event for every TERMINAL it refuses.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolBusMessage
from omnimarket.events.topics import CI_RED_TRIAGE_DECIDED_TOPIC_V1
from omnimarket.lab_work.bus import _subscribe
from omnimarket.models.ci_red_triage import (
    EnumCiRedAction,
    EnumCiRedClass,
    ModelCiRedTriageDecided,
    ci_red_decision_correlation_id,
    ci_red_owner_claim_ttl,
    ci_red_owner_run_id,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
    ModelWorkLedgerTerminalRefused,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.ci_red_claims import (
    ProjectionCiRedClaims,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.work_ledger_bus.bus import (
    WorkLedgerAppendCaller,
    WorkLedgerAppendHost,
    load_work_ledger_append_topics,
)

pytestmark = pytest.mark.unit

REDUCER_CONTRACT = Path(
    "src/omnimarket/nodes/node_pr_lifecycle_state_reducer/contract.yaml"
)
APPEND_CONTRACT = Path(
    "src/omnimarket/nodes/node_work_ledger_append_effect/contract.yaml"
)
REPO = "OmniNode-ai/omniclaude"
PR = 2606
CHECK = "branch-claim-check / branch-claim-check"
OPENED_AT = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)


def contract_ttl() -> timedelta:
    block = yaml.safe_load(REDUCER_CONTRACT.read_text())["owner_claim_lease"]
    return ci_red_owner_claim_ttl(block)


def open_claim(database: InmemoryDatabaseAdapter) -> ModelCiRedTriageDecided:
    """The bus path starts a PR-own owner; the reducer projects its CLAIM row."""
    decision_key = f"{REPO}#{PR}@head:{CHECK}"
    decided = ModelCiRedTriageDecided(
        correlation_id=ci_red_decision_correlation_id(decision_key),
        decision_key=decision_key,
        owner_key=decision_key,
        event_id="a" * 64,
        repo=REPO,
        pr_number=PR,
        head_sha="head",
        check=CHECK,
        red_class=EnumCiRedClass.PR_OWN,
        action=EnumCiRedAction.START_PR_FIX,
        action_applied=True,
        orchestrator_run_id=ci_red_owner_run_id(decision_key),
        members=(PR,),
        initial_state="ci_red:pr_own",
        evidence=f"class=pr_own check={CHECK} action=start_pr_fix",
        observed_at="2026-10-10T06:00:00Z",
    )
    HandlerPrLifecycleStateReducer().handle_dict(
        {
            **decided.model_dump(mode="json"),
            "_topic": CI_RED_TRIAGE_DECIDED_TOPIC_V1,
            "_db": database,
        }
    )
    return decided


class RecordingLedger:
    """The ledger host's file: the runner appends, the reader reads it back."""

    def __init__(self) -> None:
        self.text = "# Ledger\n"

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.text += rows if rows.endswith("\n") else rows + "\n"
        return ModelAppendCommandResult(exit_code=0, stdout="", stderr="")

    def read_text(self) -> str:
        return self.text


def terminal_append(
    lane: str, *, signer: tuple[str, Ed25519PrivateKey] | None
) -> tuple[
    ModelWorkLedgerAppendReceipt,
    list[ModelWorkLedgerTerminalRefused],
    RecordingLedger,
]:
    """The lane's TERMINAL append through the ledger host on a bus."""
    ledger = RecordingLedger()
    operator = Ed25519PrivateKey.generate()
    keys = {"operator": operator.public_key()}
    if signer is not None:
        keys[signer[0]] = signer[1].public_key()
    handler = HandlerWorkLedgerAppendEffect(
        ledger,
        ledger,
        "ledger-host",
        public_keys=keys,
        operator_principal="operator",
    )
    request_id = uuid4()
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=(
            f"2026-10-10T06:30:00Z | TERMINAL | lane={lane} | ticket=OMN-20744 "
            f"| pr={REPO}#{PR} | req={request_id} | outcome=handed-off"
        ),
        requested_by_lane=lane,
        requesting_host="lab-host",
        requested_at=OPENED_AT + timedelta(minutes=30),
    )
    refused: list[ModelWorkLedgerTerminalRefused] = []

    async def run() -> ModelWorkLedgerAppendReceipt:
        bus = EventBusInmemory(environment="local", group="ac-m6")
        await bus.start()
        host = WorkLedgerAppendHost(bus, handler)
        await host.start()

        async def on_refused(message: ProtocolBusMessage) -> None:
            payload = json.loads(message.value)["payload"]
            refused.append(ModelWorkLedgerTerminalRefused.model_validate(payload))

        unsubscribe = await _subscribe(
            bus,
            load_work_ledger_append_topics().terminal_refused,
            on_refused,
            "ac-m6-reader",
            "earliest",
        )
        caller = (
            WorkLedgerAppendCaller(bus)
            if signer is None
            else WorkLedgerAppendCaller(bus, principal=signer[0], signing_key=signer[1])
        )
        try:
            return await caller.append(request, timeout_s=5)
        finally:
            await caller.stop()
            await unsubscribe()
            await host.stop()
            await bus.close()

    return asyncio.run(run()), refused, ledger


def test_ac_m6_refused_terminal_claim_expires_past_ttl_and_unblocks_the_pr() -> None:
    database = InmemoryDatabaseAdapter()
    decided = open_claim(database)
    lane = decided.orchestrator_run_id
    assert lane is not None
    ttl = contract_ttl()

    receipt, refused, ledger = terminal_append(lane, signer=None)

    # The TERMINAL is refused for want of a signing principal and never lands,
    # so the CLAIM it would have closed is still open on the ledger.
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert "TERMINAL" not in ledger.text
    # The refusal is observable: one typed TERMINAL_REFUSED event names the lane.
    assert len(refused) == 1
    event = refused[0]
    assert event.event == "TERMINAL_REFUSED"
    assert event.request_id == receipt.request_id
    assert event.terminal_lanes == (lane,)
    assert event.tickets == ("OMN-20744",)
    assert event.prs == (f"{REPO}#{PR}",)
    assert event.reason == receipt.message

    # Past the contract TTL the owner read no longer counts the claim: the PR
    # and its cause are unblocked.
    past = ProjectionCiRedClaims(database, now=lambda: OPENED_AT + ttl)
    assert past.owned(decided.owner_key) is False
    assert past.absorbing_cause(PR, (decided.owner_key,)) is None


def test_live_claim_inside_its_ttl_still_owns_the_pr() -> None:
    database = InmemoryDatabaseAdapter()
    decided = open_claim(database)
    inside = ProjectionCiRedClaims(
        database, now=lambda: OPENED_AT + contract_ttl() - timedelta(seconds=1)
    )
    assert inside.owned(decided.owner_key) is True
    assert inside.absorbing_cause(PR, (decided.owner_key,)) == decided.owner_key


def test_signed_terminal_lands_and_emits_no_terminal_refused() -> None:
    key = Ed25519PrivateKey.generate()
    receipt, refused, ledger = terminal_append("ci-red-lane", signer=("lane", key))
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert "| TERMINAL | principal=lane | lane=ci-red-lane |" in ledger.text
    assert refused == []


def test_claim_row_lease_is_found_at_plus_contract_ttl() -> None:
    database = InmemoryDatabaseAdapter()
    decided = open_claim(database)
    claim = next(
        row
        for row in database.query("pr_lifecycle_ledger_entries", {"pr_number": PR})
        if str(row["evidence"]).startswith("claim=owner ")
    )
    assert claim["sweep_id"] != str(decided.correlation_id)
    assert datetime.fromisoformat(str(claim["next_check_at"])) == (
        OPENED_AT + contract_ttl()
    )


@pytest.mark.parametrize(
    "block",
    [
        None,
        {},
        {"ttl_seconds": None},
        {"ttl_seconds": "5400"},
        {"ttl_seconds": 0},
        {"ttl_seconds": -60},
        {"ttl_seconds": True},
        {"ttl_seconds": 5400, "grace_seconds": 60},
    ],
)
def test_missing_or_unknown_ttl_is_refused_at_load(block: object) -> None:
    with pytest.raises(ValueError, match="owner_claim_lease"):
        ci_red_owner_claim_ttl(block)


def test_reducer_refuses_a_contract_without_a_lease_ttl(tmp_path: Path) -> None:
    contract = yaml.safe_load(REDUCER_CONTRACT.read_text())
    del contract["owner_claim_lease"]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract))
    with pytest.raises(ValueError, match="owner_claim_lease"):
        HandlerPrLifecycleStateReducer(contract_path=path)


def test_contracts_declare_the_ttl_and_the_terminal_refused_topic() -> None:
    assert contract_ttl() == timedelta(seconds=5400)
    published = yaml.safe_load(APPEND_CONTRACT.read_text())["published_events"]
    topics = {entry["event_type"]: entry["topic"] for entry in published}
    assert (
        topics["ModelWorkLedgerTerminalRefused"]
        == "onex.evt.omnimarket.work-ledger-terminal-refused.v1"
        == load_work_ledger_append_topics().terminal_refused
    )
