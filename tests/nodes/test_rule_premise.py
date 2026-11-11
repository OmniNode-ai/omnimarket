# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""R-B and BatchProbe revision-five falsifiers; no production validation is replaced."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_rule_premise import (
    ModelBatchPolicy,
    ModelBatchRequest,
    ModelBriefCheckRequest,
    ModelBriefCheckResult,
    ModelBriefPremise,
    ModelPremiseRead,
    ModelPremiseValidationRequest,
)
from omnimarket.nodes.node_adoption_recovery_effect.handler import fold_findings
from omnimarket.nodes.node_rule_premise_effect import HandlerRulePremise, brief_receipt
from omnimarket.nodes.node_rule_premise_effect.handlers.batch_claim_store import (
    MemoryBatchClaimStore,
)
from omnimarket.nodes.node_rule_premise_effect.handlers.handler_batch_probe import (
    HandlerBatchProbe,
)
from omnimarket.nodes.node_rule_premise_effect.handlers.handler_rule_premise import (
    extract_premises,
)
from omnimarket.nodes.node_rule_premise_effect.handlers.send_refusal_store import (
    SendRefusalStore,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    Report,
)
from tests.nodes.rule_premise_sqlite_store import SqliteBatchClaimStore


def policy() -> ModelBatchPolicy:
    return ModelBatchPolicy.model_validate(
        yaml.safe_load(
            files("omnimarket.nodes.node_rule_premise_effect")
            .joinpath("policy.yaml")
            .read_text()
        )
    )


@dataclass
class FixtureCoreValidator:
    """Trusted core port double: only these durable read-backs exist in the fixture."""

    proofs: set[tuple[str, str, str]] = field(default_factory=set)
    verdict: str = "pass"

    def check(self, request: ModelPremiseValidationRequest) -> ModelBriefCheckResult:
        return ModelBriefCheckResult.model_validate(
            {
                "verdict": self.verdict,
                "receipt": brief_receipt(request.request.brief)
                if self.verdict == "pass"
                else "",
                "reason": "fixture core validation" if self.verdict != "pass" else "",
            }
        )

    def read_back(self, receipt_id: str, premise: str, brief_id: str) -> bool:
        return (receipt_id, premise, brief_id) in self.proofs


@dataclass
class FixtureReader:
    readable: bool = True
    merged: bool = False
    contained: bool = False
    calls: list[tuple[ModelBriefPremise, str]] = field(default_factory=list)

    def read(self, premise: ModelBriefPremise, host: str) -> ModelPremiseRead:
        self.calls.append((premise, host))
        return ModelPremiseRead(
            premise=premise,
            readable=self.readable,
            merged=self.merged,
            head="a" * 40,
            installed_revision="b" * 40 if self.contained else "",
            contained=self.contained,
            install_row="install-target-head",
            ticket_state="In Progress",
        )


def brief_request() -> ModelBriefCheckRequest:
    return ModelBriefCheckRequest(
        brief="Use repo#9 for the change.",
        lane="probe",
        ticket="OMN-19998",
        host="lab-target",
        repository="repo",
        now=100,
        batch="fixture-batch",
    )


@pytest.mark.parametrize(
    ("readable", "contained", "expected"),
    [(True, True, "pass"), (True, False, "wait"), (False, False, "UNREAD")],
)
def test_rule_premise_unmerged_pr_target_install(
    readable: bool, contained: bool, expected: str
) -> None:
    reader = FixtureReader(readable=readable, contained=contained)
    result = HandlerRulePremise(
        reader,
        FixtureCoreValidator(),
        batch_store=MemoryBatchClaimStore(),
        policy=policy(),
    ).handle(brief_request())
    assert result.verdict == expected
    assert reader.calls[0][1] == "lab-target"
    if expected == "wait":
        assert result.install_rows == ("install-target-head",)
        assert not result.receipt
    if expected == "pass":
        assert result.receipt == brief_receipt(brief_request().brief)


def test_rule_premise_merged_pr_still_needs_core_check() -> None:
    result = HandlerRulePremise(
        FixtureReader(merged=True), FixtureCoreValidator(verdict="refuse")
    ).handle(brief_request())
    assert result.verdict == "refuse"
    assert not result.receipt


def test_rule_premise_extractor_keeps_pr_url_ticket_and_heads() -> None:
    text = (
        f"repo#9 https://github.com/owner/other/pull/8 OMN-19998 head={'a' * 40} repo#9"
    )
    assert [(p.kind, p.reference) for p in extract_premises(text, "repo")] == [
        ("pr", "repo#9"),
        ("pr", "owner/other#8"),
        ("ticket", "OMN-19998"),
        ("head", "a" * 40),
    ]


@pytest.fixture(params=["memory", "sqlite"])
def store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> MemoryBatchClaimStore | SqliteBatchClaimStore:
    return (
        MemoryBatchClaimStore()
        if request.param == "memory"
        else SqliteBatchClaimStore(tmp_path / "claims.db", 10)
    )


def command(index: int, **updates: object) -> ModelBatchRequest:
    return ModelBatchRequest.model_validate(
        {
            "operation": "admit",
            "batch": "fixture-batch",
            "brief_id": f"brief-{index}",
            "premise": f"head-{index}",
            "now": 100,
            **updates,
        }
    )


def terminal(index: int, admitted: object, **updates: object) -> ModelBatchRequest:
    return command(
        index,
        operation="blocked",
        slot=admitted.slot,
        attempt=admitted.attempt,
        class_key="install-unread",
        **updates,
    )


def test_rule_premise_b1_27_synthetic_briefs_at_most_three_and_six_attempts(
    store: object,
) -> None:
    handler = HandlerBatchProbe(store, policy(), FixtureCoreValidator())
    results = [handler.handle(command(i)) for i in range(27)]
    assert sum(r.verdict == "admit" for r in results) == 3
    retry_time = 100 + policy().lease_s
    retried = [handler.handle(command(i, now=retry_time)) for i in range(27)]
    assert sum(r.verdict == "admit" for r in retried) == 3
    settled = [
        handler.handle(command(i, now=retry_time + policy().lease_s)) for i in range(27)
    ]
    assert {r.verdict for r in settled} == {"held"}
    with store.transaction("fixture-batch") as tx:
        state = tx.read()
    assert len({s.brief_id for s in state.slots}) == 3
    assert sum(s.attempt for s in state.slots) == 6
    assert len({r.escalation for r in settled}) == 1


@pytest.mark.parametrize("kind", ["different-classes", "outside-window", "timeout"])
def test_rule_premise_exhaustion_holds_without_same_class(
    store: object, kind: str
) -> None:
    selected_policy = policy().model_copy(update={"lease_s": 10000})
    handler = HandlerBatchProbe(store, selected_policy, FixtureCoreValidator())
    admitted = [handler.handle(command(i)) for i in range(3)]
    for i, result in enumerate(admitted):
        result = handler.handle(
            command(
                i,
                operation="timeout" if kind == "timeout" else "blocked",
                slot=result.slot,
                attempt=result.attempt,
                class_key=str(i) if kind == "different-classes" else "same",
                now=100 + i * (policy().window_s + 1)
                if kind == "outside-window"
                else 100,
            )
        )
    assert result.verdict == "held"
    assert handler.handle(command(10, now=10000)).escalation == result.escalation


@pytest.mark.parametrize("first_proof", [False, True])
def test_rule_premise_threshold_read_in_same_admission_before_and_after_proof(
    store: object,
    first_proof: bool,
) -> None:
    core = FixtureCoreValidator()
    handler = HandlerBatchProbe(store, policy(), core)
    admitted = [handler.handle(command(i)) for i in range(3)]
    if first_proof:
        core.proofs.add(("first-read-back", "head-2", "brief-2"))
        assert (
            handler.handle(
                command(
                    2,
                    operation="proof",
                    slot=admitted[2].slot,
                    attempt=1,
                    receipt_id="first-read-back",
                )
            ).verdict
            == "recorded"
        )
    handler.handle(terminal(0, admitted[0]))
    # Simulate an already committed second block before a separate hold worker ran.
    with store.transaction("fixture-batch") as tx:
        state = tx.read()
        slots = list(state.slots)
        slots[1] = slots[1].model_copy(
            update={
                "state": "blocked",
                "class_key": "install-unread",
                "blocked_at": 100,
            }
        )
        tx.write(state.model_copy(update={"slots": tuple(slots)}))
    assert handler.handle(command(3)).verdict == "held"
    core.proofs.add(("read-back", "head-2", "brief-2"))
    assert (
        handler.handle(
            command(
                2,
                operation="proof",
                slot=admitted[2].slot,
                attempt=1,
                receipt_id="read-back",
            )
        ).verdict
        == "held"
    )
    assert handler.handle(command(4)).verdict == "held"


def test_rule_premise_ramp_then_exhaustion_and_hold_after_ramp(store: object) -> None:
    core = FixtureCoreValidator()
    handler = HandlerBatchProbe(store, policy(), core)
    first = [handler.handle(command(i)) for i in range(3)]
    core.proofs.add(("proof-0", "head-0", "brief-0"))
    proved = handler.handle(
        command(
            0, operation="proof", slot=first[0].slot, attempt=1, receipt_id="proof-0"
        )
    )
    assert proved.capacity == policy().probe + policy().step
    ramp = [handler.handle(command(i)) for i in range(3, 10)]
    assert sum(r.verdict == "admit" for r in ramp) == policy().step
    # Different classes force the exhaustion branch rather than the threshold.
    remaining = [(i, first[i]) for i in (1, 2)] + [
        (i + 3, r) for i, r in enumerate(ramp[:3])
    ]
    for i, result in remaining:
        result = handler.handle(
            command(
                i, operation="blocked", slot=result.slot, attempt=1, class_key=str(i)
            )
        )
    assert result.verdict == "held"
    assert result.reason == "slots-exhausted"
    assert handler.handle(command(10)).escalation == result.escalation


def test_rule_premise_each_validated_proof_adds_only_one_step(store: object) -> None:
    core = FixtureCoreValidator()
    handler = HandlerBatchProbe(store, policy(), core)
    admitted = [handler.handle(command(i)) for i in range(3)]
    count = 3
    for i in range(3):
        core.proofs.add((f"proof-{i}", f"head-{i}", f"brief-{i}"))
        proof = command(
            i,
            operation="proof",
            slot=admitted[i].slot,
            attempt=1,
            receipt_id=f"proof-{i}",
        )
        result = handler.handle(proof)
        assert result.capacity == policy().probe + (i + 1) * policy().step
        assert handler.handle(proof).verdict == "refuse"
        for j in range(policy().step):
            assert handler.handle(command(count + j)).verdict == "admit"
        count += policy().step
        assert handler.handle(command(100)).verdict == "wait"
        with store.transaction("fixture-batch") as tx:
            assert len(tx.read().slots) == count


@pytest.mark.parametrize("operation", ["checkpoint", "proof"])
def test_rule_premise_checkpoint_or_unvalidated_receipt_does_not_ramp(
    store: object, operation: str
) -> None:
    handler = HandlerBatchProbe(store, policy(), FixtureCoreValidator())
    first = [handler.handle(command(i)) for i in range(3)]
    result = handler.handle(
        command(
            0,
            operation=operation,
            slot=first[0].slot,
            attempt=1,
            receipt_id="own-checkpoint",
        )
    )
    assert result.capacity == 3
    assert handler.handle(command(3)).verdict == "wait"


def test_rule_premise_crash_waits_for_lease_then_retries_same_brief_once(
    store: object,
) -> None:
    handler = HandlerBatchProbe(store, policy(), FixtureCoreValidator())
    first = [handler.handle(command(i)) for i in range(3)]
    assert (
        handler.handle(
            command(0, operation="crashed", slot=first[0].slot, attempt=1)
        ).verdict
        == "recorded"
    )
    assert handler.handle(command(0)).verdict == "wait"
    assert handler.handle(command(3, now=100 + policy().lease_s)).verdict == "wait"
    retried = handler.handle(command(0, now=100 + policy().lease_s))
    assert (retried.verdict, retried.slot, retried.attempt) == (
        "admit",
        first[0].slot,
        2,
    )
    stale = handler.handle(
        command(
            0,
            operation="blocked",
            slot=first[0].slot,
            attempt=1,
            now=100 + policy().lease_s,
        )
    )
    assert stale.verdict == "refuse"
    assert handler.handle(command(0, now=100 + 2 * policy().lease_s)).verdict != "admit"


def _admit_process(
    path: str, indices: list[int], barrier: object, queue: object
) -> None:
    handler = HandlerBatchProbe(
        SqliteBatchClaimStore(Path(path), 10), policy(), FixtureCoreValidator()
    )
    barrier.wait()
    queue.put([handler.handle(command(i)).verdict for i in indices])


def test_rule_premise_two_processes_six_admissions_claim_three_slots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lab-claims.db"
    SqliteBatchClaimStore(path, 10)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=_admit_process,
            args=(str(path), list(range(start, start + 3)), barrier, queue),
        )
        for start in (0, 3)
    ]
    for process in processes:
        process.start()
    results = queue.get(timeout=30) + queue.get(timeout=30)
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert results.count("admit") == 3
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 3


def test_rule_premise_refusal_on_real_tick_status_survives_foreground_grep() -> None:
    found = [
        (
            "missing",
            "dispatch-refused:OMN-19998",
            "lab-fill-brief UNKNOWN lab-fill-dispatch refused",
            "repair the brief",
        )
    ]
    result = fold_findings(
        {
            "lines": [],
            "status_line": "THROUGHPUT OK",
            "missing": [],
            "unknown": [],
            "checked": [],
            "alarms": ["preserve"],
        },
        found,
        Report,
    )
    # This is the actual B3 foreground filter, applied after the real print fold.
    printed = [*result["lines"], result["status_line"]]
    filtered = [
        line
        for line in printed
        if "lab-fill-brief" not in line and "UNKNOWN lab-fill-dispatch" not in line
    ]
    assert any(
        line.startswith("THROUGHPUT ") and "refused=1 tickets=OMN-19998" in line
        for line in filtered
    )
    again = fold_findings(
        result, [("missing", "later-check", "other finding", "fix")], Report
    )
    assert "refused=1 tickets=OMN-19998" in again["status_line"]
    assert again["alarms"] == ["preserve"]


def test_rule_premise_twelve_ticks_park_after_two(tmp_path: Path) -> None:
    path = tmp_path / "refusals.json"
    attempts = 0
    for tick in range(12):
        # Reopen the memory each tick; this must survive process restarts.
        memory = SendRefusalStore(path, policy())
        if not memory.parked("OMN-19998", 100 + tick):
            attempts += 1
            memory.refuse("OMN-19998", "brief-check failed", 100 + tick)
    assert attempts == 2
    assert json.loads(path.read_text())["OMN-19998"]["attempts"] == 2


def test_rule_premise_real_idle_fill_twelve_ticks_names_parked_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
        handler_idle_slot_fill as fill,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelIdleSlotFillResult,
        ModelLabFillRequest,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models.model_ruling_recovery import (
        ModelRulingRecoveryResult,
    )

    class Recovery:
        def handle(self, **kwargs: object) -> ModelRulingRecoveryResult:
            return ModelRulingRecoveryResult()

    class RefusingGate:
        calls = 0

        def handle(self, request: ModelBriefCheckRequest) -> ModelBriefCheckResult:
            self.calls += 1
            return ModelBriefCheckResult(
                verdict="refuse", reason="lab-fill-brief failed"
            )

    gate = RefusingGate()
    send = {
        "lane": "probe",
        "ticket": "OMN-19998",
        "key": "brief-key",
        "model": "codex",
        "effort": "high",
        "priority_class": "process-fix",
        "kind": "ticket",
        "repo": "repo",
        "brief": "Use repo#9",
    }
    monkeypatch.setattr(
        fill.HandlerIdleSlotFill,
        "handle",
        lambda _self, _request: ModelIdleSlotFillResult(claude_free=1, sends=[send]),
    )
    monkeypatch.setattr(fill.overlay, "repo_required_kinds", lambda: frozenset())
    monkeypatch.setattr(fill, "read_fixer_holds", lambda *_args: ({}, ""))
    monkeypatch.setattr(fill, "fill_wip_gate", lambda **_kwargs: (None, None))
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    state = tmp_path / "state"
    state.mkdir()
    backlog = state / "backlog.json"
    backlog.write_text('{"items": []}')
    for tick in range(12):
        request = ModelLabFillRequest(
            now=f"2026-11-04T19:{tick:02d}:00Z",
            request={},
            lines=[],
            launch=None,
            state_dir=state,
            backlog=str(backlog),
            runner=tmp_path / "never-run",
            out_of_scope={},
        )
        result = fill.HandlerLabFill(
            ruling_recovery=Recovery(), premise_gate=gate, batch_policy=policy()
        ).handle(request)
        refused = [
            row for row in result.found if row[1] == "dispatch-refused:OMN-19998"
        ]
        assert len(refused) == 1
        assert refused[0][0] == "missing"
        if tick >= 2:
            assert "parked" in refused[0][2]
        folded = fold_findings(
            {
                "lines": [],
                "status_line": "THROUGHPUT OK",
                "missing": [],
                "unknown": [],
                "checked": [],
            },
            result.found,
            Report,
        )
        assert "refused=1 tickets=OMN-19998" in folded["status_line"]
    assert gate.calls == 2


def test_rule_premise_raw_idle_send_refuses_no_receipt(tmp_path: Path) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        _send,
    )

    assert _send(["never-run"])[0] is False


def test_rule_premise_raw_idle_send_refuses_receipt_for_other_bytes(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        _send,
    )

    path = tmp_path / "brief.md"
    path.write_text("changed")
    assert (
        _send(
            ["never-run", "--brief", str(path)],
            brief_check_receipt=brief_receipt("old"),
            batch_slot="batch@fixture#1",
            batch_attempt=1,
        )[0]
        is False
    )


async def runtime_rule_premise_roundtrip(
    monkeypatch: pytest.MonkeyPatch, bus: object, profile: str
) -> None:
    import asyncio
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.handlers.container.container_overlay_dependencies import (
        FACTORIES,
        bind_overlay,
        load_overlay_bindings,
    )

    package = files("omnimarket.nodes.node_rule_premise_effect")
    contract = yaml.safe_load(package.joinpath("contract.yaml").read_text())
    manifest = discover_contracts_from_paths(
        [Path(str(package.joinpath("contract.yaml")))]
    )
    assert manifest.errors == ()
    assert len(manifest.contracts) == 1
    core = FixtureCoreValidator()
    claims = MemoryBatchClaimStore()
    monkeypatch.setitem(
        FACTORIES,
        "part_a_premise_reader",
        lambda _config: FixtureReader(contained=True),
    )
    monkeypatch.setitem(FACTORIES, "core_premise_validator", lambda _config: core)
    monkeypatch.setitem(FACTORIES, "memory_batch_claim_store", lambda _config: claims)
    monkeypatch.setitem(FACTORIES, "part_a_batch_claim_store", lambda _config: claims)
    bind_overlay(
        load_overlay_bindings(Path(str(package.joinpath(f"overlay.{profile}.yaml"))))
    )
    completed_topic = "onex.evt.omnimarket.rule-premise-completed.v1"
    assert contract["terminal_event"] == completed_topic
    answers: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    await bus.start()

    async def collect(message: object) -> None:
        await answers.put(json.loads(message.value))

    try:
        await bus.subscribe(
            completed_topic, group_id="premise-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest, engine, event_bus=bus, environment="local"
        )
        assert [row.outcome.value for row in report.results] == ["wired"]
        engine.freeze()
        for request in (brief_request(), command(0)):
            correlation = uuid4()
            envelope = ModelEventEnvelope[object](
                payload=request.model_dump(mode="json"),
                correlation_id=correlation,
                event_type=type(request).__name__,
            )
            await bus.publish(
                contract["runtime_dispatch"]["command_topic"],
                None,
                envelope.model_dump_json().encode(),
                None,
            )
            answer = await asyncio.wait_for(answers.get(), timeout=5)
            assert answer["correlation_id"] == str(correlation)
            assert answer["payload"]["verdict"] in {"pass", "admit"}
    finally:
        bind_overlay(None)
        await bus.close()


async def test_rule_premise_packaged_node_wires_both_operations_on_inmemory_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    await runtime_rule_premise_roundtrip(
        monkeypatch,
        EventBusInmemory(environment="local", group="rule-premise-test"),
        "inmemory",
    )
