"""Red CI starts the existing scoped sweep and projects one decision per head."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _topics_for_handler_entry

from omnimarket.events.topics import (
    CI_RED_TRIAGE_DECIDED_TOPIC_V1,
    CI_RUN_FAILED_TOPIC_V1,
    PR_LIFECYCLE_ORCHESTRATOR_START_TOPIC_V1,
)
from omnimarket.models.ci_red_triage import (
    EnumCiRedAction,
    EnumCiRedClass,
    ModelCiRedFacts,
    ModelCiRedPeer,
    ModelCiRedTriageDecided,
    ModelCiRunFailedEvent,
    ci_run_failed_event_id,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_ci_red_triage import (
    GhCiRedFactsReader,
    HandlerCiRedTriage,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ModelPrLifecycleStartCommand,
)
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_ci_red import (
    HandlerDetectCiRed,
)

CHECK = "branch-claim-check / branch-claim-check"


def event(
    pr: int = 2606,
    *,
    armed: bool = True,
    peers: bool = False,
    checks: tuple[str, ...] = (CHECK,),
) -> ModelCiRunFailedEvent:
    checks = tuple(sorted(checks))
    return ModelCiRunFailedEvent(
        event_id=ci_run_failed_event_id("omniclaude", pr, f"head-{pr}", checks),
        repo="omniclaude",
        pr_number=pr,
        head_sha=f"head-{pr}",
        base="main",
        armed=armed,
        queued=False,
        failing_checks=checks,
        ci_read_at="2026-10-08T10:00:00Z",
        observed_at="2026-10-08T10:00:00Z",
        source_digest="d" * 64,
        peers=tuple(
            ModelCiRedPeer(
                pr_number=n, head_sha=f"head-{n}", armed=True, red_contexts=checks
            )
            for n in (2606, 2607, 2608)
            if n != pr
        )
        if peers
        else (),
    )


class FakeFactsReader:
    def __init__(
        self,
        *,
        conclusions: dict[str, str] | None = None,
        base: tuple[str, ...] = (),
        fail: bool = False,
    ) -> None:
        self.conclusions = conclusions or {}
        self.base = base
        self.fail = fail

    def read(self, event: ModelCiRunFailedEvent) -> ModelCiRedFacts:
        if self.fail:
            raise RuntimeError("unread GitHub")
        return ModelCiRedFacts(
            event=event,
            check_conclusions=self.conclusions,
            base_red_checks=self.base,
            base_read=not self.fail,
        )


@pytest.mark.asyncio
async def test_shadow_mode_records_decision_and_starts_nothing() -> None:
    handler = HandlerCiRedTriage(facts_reader=FakeFactsReader(), act=False)
    outputs = [await handler.handle(event(n, peers=True)) for n in (2606, 2607, 2608)]
    emitted = [ev for output in outputs for ev in output.events]
    starts = [ev for ev in emitted if isinstance(ev, ModelPrLifecycleStartCommand)]
    decisions = [ev for ev in emitted if isinstance(ev, ModelCiRedTriageDecided)]
    assert len(starts) == 0
    assert len(decisions) == 3
    assert [ev.action for ev in decisions] == [
        EnumCiRedAction.START_CAUSE_OWNER,
        EnumCiRedAction.START_CAUSE_OWNER,
        EnumCiRedAction.START_CAUSE_OWNER,
    ]
    assert handler._owners == {}
    assert all(ev.action_applied is False for ev in decisions)
    run_id = decisions[0].orchestrator_run_id
    assert run_id is not None
    assert all(ev.orchestrator_run_id == run_id for ev in decisions)
    assert "start=withheld:act=false" in decisions[0].evidence


@pytest.mark.asyncio
async def test_contract_defaults_to_shadow_mode_and_starts_nothing() -> None:
    handler = HandlerCiRedTriage(facts_reader=FakeFactsReader())
    assert handler._act is False
    output = await handler.handle(event())
    assert not any(isinstance(ev, ModelPrLifecycleStartCommand) for ev in output.events)
    assert len(output.events) == 1
    decision = output.events[0]
    assert isinstance(decision, ModelCiRedTriageDecided)
    assert decision.action_applied is False
    assert "start=withheld:act=false" in decision.evidence


@pytest.mark.asyncio
async def test_ac3_three_shared_reds_have_one_owner_start() -> None:
    handler = HandlerCiRedTriage(facts_reader=FakeFactsReader(), act=True)
    outputs = [await handler.handle(event(n, peers=True)) for n in (2606, 2607, 2608)]
    emitted = [ev for output in outputs for ev in output.events]
    starts = [ev for ev in emitted if isinstance(ev, ModelPrLifecycleStartCommand)]
    decisions = [ev for ev in emitted if isinstance(ev, ModelCiRedTriageDecided)]
    assert len(starts) == 1
    assert len(decisions) == 3
    start = starts[0]
    owner_key = (
        "cause:OmniNode-ai/omniclaude:"
        + hashlib.sha256(CHECK.encode()).hexdigest()[:12]
    )
    assert (
        start.run_id == "ci-red-" + hashlib.sha256(owner_key.encode()).hexdigest()[:16]
    )
    assert start.correlation_id == uuid5(
        NAMESPACE_URL, "onex:ci-red-owner:" + owner_key
    )
    assert start.repos == "OmniNode-ai/omniclaude"
    assert start.pr_numbers == (2606, 2607, 2608)
    assert start.dry_run is False
    assert start.fix_only is True
    assert start.action_mode == "report_only"
    assert start.merge_queue_mutation_kill_switch is True
    assert [ev.action for ev in decisions] == [
        EnumCiRedAction.START_CAUSE_OWNER,
        EnumCiRedAction.JOINED_OWNER,
        EnumCiRedAction.JOINED_OWNER,
    ]
    assert [ev.action_applied for ev in decisions] == [True, False, False]
    assert all(
        ev.red_class == EnumCiRedClass.SHARED_CAUSE
        and ev.orchestrator_run_id == start.run_id
        for ev in decisions
    )
    assert all(ev.observed_at == event().observed_at for ev in decisions)


@pytest.mark.asyncio
async def test_ac4_duplicate_emits_nothing() -> None:
    handler = HandlerCiRedTriage(facts_reader=FakeFactsReader(), act=True)
    first = await handler.handle(event().model_dump(mode="json"))
    second = await handler.handle(event())
    assert len(first.events) == 2
    assert second.events == ()


@pytest.mark.asyncio
async def test_ac4_event_id_replay_skips_changed_or_failing_facts() -> None:
    class ChangingFactsReader:
        calls = 0

        def read(self, red: ModelCiRunFailedEvent) -> ModelCiRedFacts:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("facts became unreadable")
            return ModelCiRedFacts(
                event=red, base_read=True, base_red_checks=red.failing_checks
            )

    reader = ChangingFactsReader()
    handler = HandlerCiRedTriage(facts_reader=reader, act=True)
    # With readable base facts the deciding check is 'a'; unread facts would
    # select the peer cluster's 'b' and produce a different decision_key.
    red = event(checks=("a", "b")).model_copy(
        update={
            "peers": tuple(
                ModelCiRedPeer(
                    pr_number=n, head_sha=f"head-{n}", armed=True, red_contexts=("b",)
                )
                for n in (2607, 2608)
            )
        }
    )
    emitted = [
        ev
        for output in (await handler.handle(red), await handler.handle(red))
        for ev in output.events
    ]
    decisions = [ev for ev in emitted if isinstance(ev, ModelCiRedTriageDecided)]
    starts = [ev for ev in emitted if isinstance(ev, ModelPrLifecycleStartCommand)]
    assert len(decisions) == 1
    assert decisions[0].check == "a"
    assert decisions[0].red_class == EnumCiRedClass.DEV_HEAD
    assert len(starts) == 1
    assert reader.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("armed", [True, False])
async def test_pr_own_and_unarmed(armed: bool) -> None:
    output = await HandlerCiRedTriage(facts_reader=FakeFactsReader(), act=True).handle(
        event(armed=armed)
    )
    decision = output.events[-1]
    assert decision.red_class == EnumCiRedClass.PR_OWN
    assert decision.action == (
        EnumCiRedAction.START_PR_FIX if armed else EnumCiRedAction.RECORD_ONLY
    )
    assert decision.action_applied is armed
    assert len(output.events) == (2 if armed else 1)
    assert decision.correlation_id == uuid5(
        NAMESPACE_URL, "onex:ci-red-decision:" + decision.decision_key
    )
    if armed:
        assert output.events[0].pr_numbers == (2606,)
    else:
        assert decision.orchestrator_run_id is None


@pytest.mark.asyncio
async def test_facts_reader_raising_is_unread_conservative_pr_fix() -> None:
    output = await HandlerCiRedTriage(facts_reader=FakeFactsReader(fail=True)).handle(
        event()
    )
    decision = output.events[-1]
    assert decision.red_class == EnumCiRedClass.PR_OWN
    assert f"unread=head conclusions: {CHECK}; base checks" in decision.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("facts", "action"),
    [
        (
            FakeFactsReader(conclusions={CHECK: "timed_out"}),
            EnumCiRedAction.RERUN_FAILED,
        ),
        (FakeFactsReader(base=(CHECK,)), EnumCiRedAction.START_DEV_CAUSE),
    ],
)
async def test_runner_and_dev_owner_starts(
    facts: FakeFactsReader, action: EnumCiRedAction
) -> None:
    output = await HandlerCiRedTriage(facts_reader=facts, act=True).handle(event())
    assert output.events[0].dry_run is False
    assert output.events[0].pr_numbers == (2606,)
    assert output.events[-1].action == action


@pytest.mark.asyncio
async def test_unarmed_observation_does_not_claim_owner() -> None:
    handler = HandlerCiRedTriage(facts_reader=FakeFactsReader(), act=True)
    unarmed = await handler.handle(event(2606, peers=True, armed=False))
    armed = await handler.handle(event(2607, peers=True))
    assert unarmed.events[-1].action == EnumCiRedAction.RECORD_ONLY
    assert armed.events[-1].action == EnumCiRedAction.START_CAUSE_OWNER


@pytest.mark.asyncio
async def test_bounded_owner_and_decision_caches() -> None:
    handler = HandlerCiRedTriage(facts_reader=FakeFactsReader(), act=True)
    handler.MEMORY_LIMIT = 2
    for n in (1, 2, 1, 3):
        await handler.handle(event(n))
    assert (
        len(handler._owners) == len(handler._decided) == len(handler._seen_events) == 2
    )
    assert (await handler.handle(event(1))).events == ()
    joined = await handler.handle(event(2))
    assert len(joined.events) == 1
    assert joined.events[0].action == EnumCiRedAction.JOINED_OWNER


@pytest.mark.parametrize(
    "node", ["node_pr_lifecycle_orchestrator", "node_pr_state_emit_effect"]
)
def test_contract_routes_dispatch_to_exact_topics(node: str) -> None:
    path = Path("src/omnimarket/nodes") / node / "contract.yaml"
    raw = yaml.safe_load(path.read_text())
    manifest = discover_contracts_from_paths([path])
    assert not manifest.errors
    contract = manifest.contracts[0]
    assert contract.handler_routing is not None
    assert "db_io" not in raw
    for raw_entry, entry in zip(
        raw["handler_routing"]["handlers"],
        contract.handler_routing.handlers,
        strict=True,
    ):
        assert raw_entry.get("topic") or raw_entry.get("event_type")
        topics = _topics_for_handler_entry(contract, entry)
        assert len(topics) == 1
        assert topics[0] in raw["event_bus"]["subscribe_topics"]
    declared = {row["event_type"]: row["topic"] for row in raw["published_events"]}
    handler = (
        HandlerCiRedTriage if node.endswith("orchestrator") else HandlerDetectCiRed
    )
    for model, topic in handler.published_event_topics.items():
        assert declared[model.__name__.removeprefix("Model")] == topic
        assert topic in raw["event_bus"]["publish_topics"]
    if node.endswith("orchestrator"):
        assert raw["ci_red_triage"]["act"] is False
        assert CI_RUN_FAILED_TOPIC_V1 in raw["event_bus"]["subscribe_topics"]
        assert CI_RED_TRIAGE_DECIDED_TOPIC_V1 in raw["event_bus"]["publish_topics"]
        assert (
            PR_LIFECYCLE_ORCHESTRATOR_START_TOPIC_V1
            in raw["event_bus"]["publish_topics"]
        )


def test_compute_routes_only_batch_triage_and_declares_pure_ci_classifier() -> None:
    path = Path("src/omnimarket/nodes/node_pr_lifecycle_triage_compute/contract.yaml")
    raw = yaml.safe_load(path.read_text())
    manifest = discover_contracts_from_paths([path])
    assert not manifest.errors
    contract = manifest.contracts[0]
    assert contract.handler_routing is not None
    assert [entry.operation for entry in contract.handler_routing.handlers] == [
        "triage_prs"
    ]
    assert "topic" not in raw["handler_routing"]["handlers"][0]
    assert _topics_for_handler_entry(
        contract, contract.handler_routing.handlers[0]
    ) == ("onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1",)
    assert raw["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.pr-lifecycle-triage-completed.v1"
    ]
    assert "published_events" not in raw
    operations = {entry["name"]: entry for entry in raw["operations"]}
    assert "detect_ci_red" not in operations
    classifier = operations["classify_ci_red"]
    assert classifier["input_model"] == {
        "name": "ModelCiRedFacts",
        "module": "omnimarket.models.ci_red_triage",
    }
    assert classifier["output_model"] == {
        "name": "ModelCiRedClassification",
        "module": "omnimarket.models.ci_red_triage",
    }
    assert classifier["handler"]["name"] == "HandlerClassifyCiRed"


def test_gh_reader_newest_checks_and_get_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert kwargs["timeout"] == 20
        if args[0:3] == ["gh", "api", "graphql"]:
            assert args[3] == "-f"
            assert args[4].startswith("query=query { ")
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {}}))
        assert args[0:4] == ["gh", "api", "--method", "GET"]
        runs = [
            {
                "id": 1,
                "name": CHECK,
                "started_at": "2026-10-08T10:00:00Z",
                "conclusion": "failure",
            },
            {
                "id": 2,
                "name": CHECK,
                "started_at": "2026-10-08T10:00:00Z",
                "conclusion": "success",
            },
            {
                "id": 3,
                "name": "slow",
                "started_at": "2026-10-08T11:00:00Z",
                "conclusion": "timed_out",
            },
        ]
        return subprocess.CompletedProcess(args, 0, json.dumps({"check_runs": runs}))

    monkeypatch.setattr(subprocess, "run", run)
    facts = GhCiRedFactsReader().read(event())
    assert facts.check_conclusions == {CHECK: "success", "slow": "timed_out"}
    assert facts.base_read is True
    assert facts.base_red_checks == ("slow",)
    assert (
        calls[0][-1]
        == "repos/OmniNode-ai/omniclaude/commits/head-2606/check-runs?per_page=100"
    )
    assert (
        calls[1][-1]
        == "repos/OmniNode-ai/omniclaude/commits/main/check-runs?per_page=100"
    )
    # The annotation read is a GraphQL query, never a mutation; a PR absent from it is unread.
    assert calls[2][0:3] == ["gh", "api", "graphql"]
    assert "mutation" not in calls[2][4]
    assert len(calls) == 3
    assert facts.annotations_read is False


def graphql_node(head: str, annotations: dict[str, str]) -> dict[str, Any]:
    contexts = [
        {
            "name": name,
            "conclusion": "FAILURE",
            "annotations": {
                "nodes": [{"annotationLevel": "FAILURE", "message": text}]
                if text
                else []
            },
        }
        for name, text in annotations.items()
    ]
    return {
        "number": 0,
        "headRefOid": head,
        "statusCheckRollup": {
            "nodes": [
                {"commit": {"statusCheckRollup": {"contexts": {"nodes": contexts}}}}
            ]
        },
    }


def test_gh_reader_reads_annotations_of_pr_and_armed_peers_at_their_heads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peers = tuple(
        ModelCiRedPeer(
            pr_number=n, head_sha=f"head-{n}", armed=n != 9, red_contexts=(CHECK,)
        )
        for n in range(3, 10)
    )
    ev = event(3000).model_copy(update={"peers": peers})
    queries: list[str] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0:3] != ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"check_runs": []}))
        query = args[4]
        queries.append(query)
        numbers = [
            int(part.split(")")[0]) for part in query.split("pullRequest(number: ")[1:]
        ]
        data = {}
        for i, number in enumerate(numbers):
            # PR 8 moved to a new head since the event: its read is stale, so unread.
            head = "moved" if number == 8 else f"head-{number}"
            data[f"a{i}"] = {
                "pullRequest": graphql_node(
                    head, {CHECK: f"boom {number}", "other": ""}
                )
            }
        return subprocess.CompletedProcess(args, 0, json.dumps({"data": data}))

    monkeypatch.setattr(subprocess, "run", run)
    facts = GhCiRedFactsReader().read(ev)
    assert facts.annotations_read is True
    assert facts.annotations == {CHECK: "boom 3000", "other": ""}
    assert sorted(facts.peer_annotations) == [3, 4, 5, 6, 7]
    assert facts.peer_annotations[4] == {CHECK: "boom 4", "other": ""}
    # Seven PRs (the unarmed peer 9 is never read) in chunks of six.
    assert len(queries) == 2
    assert "pullRequest(number: 9)" not in "".join(queries)
    assert facts.base_read is True


def test_gh_reader_failed_annotation_read_keeps_check_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0:3] == ["gh", "api", "graphql"]:
            raise subprocess.TimeoutExpired(args, 20)
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {"check_runs": [{"id": 1, "name": CHECK, "conclusion": "failure"}]}
            ),
        )

    monkeypatch.setattr(subprocess, "run", run)
    facts = GhCiRedFactsReader().read(event())
    assert facts.check_conclusions == {CHECK: "failure"}
    assert facts.base_read is True
    assert facts.annotations_read is False
    assert facts.annotations == {}


def test_gh_reader_any_error_discards_partial_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.TimeoutExpired(args, 20)
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {"check_runs": [{"id": 1, "name": CHECK, "conclusion": "failure"}]}
            ),
        )

    monkeypatch.setattr(subprocess, "run", run)
    facts = GhCiRedFactsReader().read(event())
    assert facts.check_conclusions == {}
    assert facts.base_red_checks == ()
    assert facts.base_read is False


@pytest.mark.asyncio
@pytest.mark.parametrize("numbers", [(2606, 2607), [2606, 2607], "2606, 2607"])
async def test_command_accepts_pr_numbers_and_inventories_only_selection(
    numbers: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
        PrRecord,
    )
    from tests.test_golden_chain_pr_lifecycle_orchestrator import (
        MockInventory,
        _make_orchestrator,
    )

    inventory = MockInventory(
        prs=(PrRecord(repo="OmniNode-ai/omniclaude", pr_number=999),)
    )
    orchestrator = await _make_orchestrator(inventory=inventory)

    def forbidden(repo: str) -> tuple[int, ...]:
        pytest.fail("scoped start must not enumerate open PRs")

    monkeypatch.setattr(orchestrator, "_enumerate_open_pr_numbers", forbidden)
    command = ModelPrLifecycleStartCommand(
        correlation_id=uuid5(NAMESPACE_URL, "inventory-test"),
        run_id="ci-red-test",
        repos="omniclaude",
        pr_numbers=numbers,
        inventory_only=True,
        loop_until_done=False,
    )
    result = await orchestrator.handle(command)
    assert result.error_message is None
    assert command.pr_numbers == (2606, 2607)
    assert inventory.call_count == 1
    assert inventory.last_input.pr_numbers == (2606, 2607)
    assert inventory.last_input.repo == "OmniNode-ai/omniclaude"


@pytest.mark.asyncio
async def test_selection_requires_one_repo() -> None:
    from tests.test_golden_chain_pr_lifecycle_orchestrator import _make_orchestrator

    orchestrator = await _make_orchestrator()
    with pytest.raises(ValueError, match="exactly one repo"):
        await orchestrator._call_inventory(
            repos=("a", "b"), dry_run=True, selected_pr_numbers=(1,)
        )


@pytest.mark.asyncio
async def test_runtime_publishes_red_and_decision_then_reducer_projects() -> None:
    """Exercise actual output normalization and topic resolution for both node kinds."""
    from types import SimpleNamespace

    from omnibase_core.enums.enum_node_kind import EnumNodeKind
    from omnibase_core.runtime.runtime_fanout_resolver import resolve_published_topic
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _normalize_handler_result,
    )
    from omnibase_infra.runtime.service_dispatch_result_applier import (
        build_contract_result_applier,
    )

    from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
        HandlerPrLifecycleStateReducer,
    )
    from omnimarket.projection.pr_ledger_projection import PR_LEDGER_PROJECTION_TABLE
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
    from tests.unit.nodes.node_pr_state_emit_effect.test_detect_ci_red import (
        wire,
    )

    envelope = SimpleNamespace(
        payload={}, correlation_id=uuid5(NAMESPACE_URL, "runtime-test")
    )
    emit_contract = yaml.safe_load(
        Path("src/omnimarket/nodes/node_pr_state_emit_effect/contract.yaml").read_text()
    )
    orchestrator_contract = yaml.safe_load(
        Path(
            "src/omnimarket/nodes/node_pr_lifecycle_orchestrator/contract.yaml"
        ).read_text()
    )
    detector = HandlerDetectCiRed()
    detected = await detector.handle(wire())
    normalized = _normalize_handler_result(
        detected, envelope, None, EnumNodeKind.EFFECT
    )
    assert normalized is not None
    assert len(normalized.output_events) == 1
    red = normalized.output_events[0]
    published = {
        row["event_type"]: row["topic"] for row in emit_contract["published_events"]
    }
    assert resolve_published_topic(published, red) == CI_RUN_FAILED_TOPIC_V1

    class RecordingBus:
        def __init__(self) -> None:
            self.topics: list[str] = []

        async def publish_envelope(
            self, envelope: object, topic: str, *, key: bytes | None = None
        ) -> None:
            self.topics.append(topic)

    bus = RecordingBus()
    applier = build_contract_result_applier(
        event_bus=bus,
        contract_path=Path(
            "src/omnimarket/nodes/node_pr_state_emit_effect/contract.yaml"
        ),
        publish_topics=emit_contract["event_bus"]["publish_topics"],
        terminal_event=emit_contract["terminal_event"],
    )
    await applier.apply(normalized)
    assert bus.topics == [CI_RUN_FAILED_TOPIC_V1]

    handler = HandlerCiRedTriage(facts_reader=FakeFactsReader(), act=True)
    output = await handler.handle(red.model_dump(mode="json"))
    normalized = _normalize_handler_result(
        output, envelope, None, EnumNodeKind.ORCHESTRATOR
    )
    assert normalized is not None
    published = {
        row["event_type"]: row["topic"]
        for row in orchestrator_contract["published_events"]
    }
    topics = [resolve_published_topic(published, ev) for ev in normalized.output_events]
    assert topics == [
        PR_LIFECYCLE_ORCHESTRATOR_START_TOPIC_V1,
        CI_RED_TRIAGE_DECIDED_TOPIC_V1,
    ]
    database = InmemoryDatabaseAdapter()
    decision = normalized.output_events[-1]
    HandlerPrLifecycleStateReducer().handle_dict(
        {**decision.model_dump(mode="json"), "_db": database, "_topic": topics[-1]}
    )
    row = database.query(PR_LEDGER_PROJECTION_TABLE)[0]
    assert row["initial_state"] == "ci_red:pr_own"
    assert row["final_state"] == "fix_dispatched"
    assert row["evidence"] == decision.evidence

    dropped = await detector.handle(wire(ci_read_at="2026-10-08T11:00:00Z"))
    normalized = _normalize_handler_result(dropped, envelope, None, EnumNodeKind.EFFECT)
    assert normalized is not None
    assert normalized.output_count == 0
    assert normalized.output_events == []
    await applier.apply(normalized)
    assert bus.topics == [CI_RUN_FAILED_TOPIC_V1]

    green = await detector.handle(wire(2607, ci_verdict="GREEN", red_contexts=[]))
    normalized = _normalize_handler_result(green, envelope, None, EnumNodeKind.EFFECT)
    assert normalized is not None
    await applier.apply(normalized)
    assert bus.topics == [CI_RUN_FAILED_TOPIC_V1]
