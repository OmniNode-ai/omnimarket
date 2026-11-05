# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routing falsifiers; the nine synthetic cases do not replace the recorded B2 corpus."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.model_lane_routing import (
    ModelBlockedLane,
    ModelLaneBlockFacts,
    ModelLaneRoutingItem,
    ModelLaneRoutingOverlay,
    ModelLaneRoutingRequest,
    ModelOwnerMap,
    ModelOwnerRule,
)
from omnimarket.nodes.node_lane_block_diagnose_compute.handler import (
    HandlerLaneBlockDiagnoseCompute,
)
from omnimarket.nodes.node_lane_block_diagnose_effect.adapters.adapter_routing_store import (
    HandlerLaneRoutingStore,
)
from omnimarket.nodes.node_lane_block_diagnose_effect.handler import (
    HandlerLaneBlockDiagnoseEffect,
)


@pytest.fixture
def owner_map():
    return ModelOwnerMap(
        rules=(
            ModelOwnerRule(
                repository="repo-a", paths=("repo-a/",), packages=("package_a",)
            ),
            ModelOwnerRule(
                repository="repo-b", paths=("repo-b/",), packages=("package_b",)
            ),
            ModelOwnerRule(
                repository="repo-c", paths=("repo-c/",), packages=("package_c",)
            ),
        )
    )


class Gateway:
    """Receipted, idempotent downstream ports; admission lives in core."""

    def __init__(self):
        self.commands = {}
        self.bindings = {}
        self.exceptions = {}
        self.triages = {}
        self.tickets = {"ticket-b": "repo-b"}
        self.refusal = None
        self.fail_after_dispatch = False
        self.admissions = []

    def diagnose(self, facts):
        return HandlerLaneBlockDiagnoseCompute().handle(facts)

    def admit(self, command, owners):
        self.admissions.append(command)
        return self.refusal

    def fill(self, command):
        self.commands.setdefault(command.key, command)
        if self.fail_after_dispatch:
            self.fail_after_dispatch = False
            raise RuntimeError("reply lost after durable send")
        return "owner-" + command.key

    def bind(self, key, lane, items):
        self.bindings.setdefault(lane, set()).update(items)

    def exception(self, key, evidence, reason):
        self.exceptions[key] = (evidence, reason)

    def ticket_repository(self, ticket):
        return self.tickets.get(ticket)

    def triage(self, request):
        self.triages.setdefault(request.key, request)
        return "triage-" + request.key


@pytest.fixture
def harness(tmp_path, owner_map):
    report_root = tmp_path / "reports"
    report_root.mkdir()
    repository_root = tmp_path / "repos"
    repository_root.mkdir()
    for repo in ("repo-a", "repo-b", "repo-c"):
        path = repository_root / repo / "src" / "producer.py"
        path.parent.mkdir(parents=True)
        path.write_text("def producer():\n    raise RuntimeError('fixture')\n")
    overlay = ModelLaneRoutingOverlay(
        owner_map=owner_map,
        report_roots=(str(report_root),),
        repository_roots=(str(repository_root),),
        state_path=str(tmp_path / "state.json"),
        max_open_triage=1,
        max_report_bytes=10000,
        max_file_bytes=1000,
        max_named_files=8,
    )
    gateway = Gateway()
    store = HandlerLaneRoutingStore(Path(overlay.state_path))
    handler = HandlerLaneBlockDiagnoseEffect(overlay, gateway, store)
    return handler, gateway, store, report_root, overlay


def blocked(
    report_root,
    number=1,
    *,
    text="Producer lives in repo-b/src/producer.py:12",
    item=None,
    cls=None,
):
    report = report_root / f"lane-{number}.md"
    report.write_text(text)
    return ModelLaneRoutingRequest(
        operation="diagnose",
        terminal=ModelBlockedLane(
            lane=f"lane-{number}",
            repository="repo-a",
            outcome="blocked",
            blocker=text,
            report_path=str(report),
            class_key=cls or f"class-{number}",
            head="head-1",
            item_ids=(item or f"item-{number}",),
            brief="Original task",
        ),
    )


def test_lane_block_diagnose_routes_to_evidenced_owner_and_attaches_report(harness):
    handler, gateway, store, reports, _ = harness
    request = blocked(reports)
    result = handler.handle(request)
    assert result.status == "dispatched"
    (command,) = gateway.commands.values()
    assert command.contract == "fill-backlog-item"
    assert command.target_repo == "repo-b"
    assert command.report_text == request.terminal.blocker
    assert command.report_text in command.brief
    assert "for-item=item-1" in command.brief
    (claim,) = store.read().claims.values()
    assert claim.item_ids == ("item-1",)
    assert gateway.bindings[claim.lane] == {"item-1"}
    assert result.diagnosis.evidence_line == request.terminal.blocker
    assert result.read_files == ("repo-b/src/producer.py",)


def test_lane_block_diagnose_nine_synthetic_results_and_second_run(harness):
    handler, gateway, _, reports, _ = harness
    requests = [blocked(reports, i) for i in range(7)]
    requests += [blocked(reports, 7, text="Producer lives in repo-a/src/producer.py:2")]
    requests += [blocked(reports, 8, text="Original error and reproducer are missing")]
    results = [handler.handle(request) for request in requests]
    assert [r.status for r in results] == ["dispatched"] * 7 + [
        "same-owner",
        "exception",
    ]
    assert len(gateway.commands) == 7
    assert len(gateway.exceptions) == 1
    for request in requests:
        assert handler.handle(request).status != "dispatched"
    assert len(gateway.commands) == 7


def test_lane_block_diagnose_shared_claim_binds_every_originating_item(harness):
    handler, gateway, store, reports, _ = harness
    handler.handle(blocked(reports, 1, item="first", cls="one-cause"))
    result = handler.handle(blocked(reports, 2, item="second", cls="one-cause"))
    assert result.status == "joined"
    assert len(gateway.commands) == 1
    (claim,) = store.read().claims.values()
    assert set(claim.item_ids) == {"first", "second"}
    assert gateway.bindings[claim.lane] == {"first", "second"}


def test_lane_block_diagnose_restarts_claim_store_and_recovers_lost_dispatch_reply(
    harness,
):
    handler, gateway, store, reports, overlay = harness
    gateway.fail_after_dispatch = True
    request = blocked(reports)
    with pytest.raises(RuntimeError, match="reply lost"):
        handler.handle(request)
    assert len(store.read().claims) == 1
    restarted = HandlerLaneBlockDiagnoseEffect(
        overlay, gateway, HandlerLaneRoutingStore(Path(overlay.state_path))
    )
    assert restarted.handle(request).status == "dispatched"
    assert len(gateway.commands) == 1
    assert restarted.handle(request).status == "joined"


def test_lane_block_diagnose_head_is_part_of_shared_claim_key(harness):
    handler, gateway, _, reports, _ = harness
    request = blocked(reports)
    handler.handle(request)
    handler.handle(
        request.model_copy(
            update={"terminal": request.terminal.model_copy(update={"head": "head-2"})}
        )
    )
    assert len(gateway.commands) == 2


@pytest.mark.parametrize(
    "text",
    [
        "The fix probably belongs elsewhere",
        "package_but_not_package_b is mentioned",
        "Read repo-bogus/src/producer.py",
        "repo-b/src/producer.py and repo-c/src/producer.py",
    ],
)
def test_lane_block_diagnose_never_guesses(owner_map, text):
    result = HandlerLaneBlockDiagnoseCompute().handle(
        ModelLaneBlockFacts(
            blocker=text,
            report_text=text,
            owner_map=owner_map,
        )
    )
    assert result.owner is None
    assert result.confidence == 0
    assert result.reason


def test_lane_block_diagnose_package_and_traceback(owner_map):
    compute = HandlerLaneBlockDiagnoseCompute()
    for text in (
        "ModuleNotFoundError: No module named 'package_b.worker'",
        'File "/checkout/repo-b/src/producer.py", line 12',
    ):
        result = compute.handle(
            ModelLaneBlockFacts(blocker=text, report_text=text, owner_map=owner_map)
        )
        assert result.owner == "repo-b"
        assert result.evidence_line == text


def test_lane_block_diagnose_prose_queues_exception(harness):
    handler, gateway, _, reports, _ = harness
    result = handler.handle(
        blocked(reports, text="The original finding was not attached")
    )
    assert result.status == "exception"
    assert result.diagnosis.owner is None
    assert len(gateway.exceptions) == 1
    assert not gateway.commands


def test_lane_block_diagnose_new_item_uses_bounded_read_only_triage(harness):
    handler, gateway, _, _, _ = harness
    first = ModelLaneRoutingRequest(
        operation="initial",
        item=ModelLaneRoutingItem(item_id="new-1", text="A new ask"),
    )
    result = handler.handle(first)
    assert result.status == "triage"
    (triage,) = gateway.triages.values()
    assert triage.allow_code is False
    assert triage.worker is False
    assert triage.target_repo is None
    assert not gateway.commands
    assert handler.handle(first).status == "triage"
    second = ModelLaneRoutingRequest(
        operation="initial",
        item=ModelLaneRoutingItem(item_id="new-2", text="Another ask"),
    )
    assert handler.handle(second).status == "triage-cap"
    assert len(gateway.triages) == 1


def test_lane_block_diagnose_initial_ticket_repository_overrides_table_row(harness):
    handler, gateway, _, _, _ = harness
    request = ModelLaneRoutingRequest(
        operation="initial",
        item=ModelLaneRoutingItem(
            item_id="new-1",
            text="A ticket item",
            repository="repo-a",
            ticket="ticket-b",
            head="ticket-head",
        ),
    )
    assert handler.handle(request).status == "dispatched"
    (command,) = gateway.commands.values()
    assert command.target_repo == "repo-b"
    assert not gateway.triages


def test_lane_block_diagnose_unknown_ticket_repository_has_no_fallback(harness):
    handler, gateway, _, _, _ = harness
    request = ModelLaneRoutingRequest(
        operation="initial",
        item=ModelLaneRoutingItem(
            item_id="new-1",
            text="A ticket item",
            repository="repo-a",
            ticket="unreadable-ticket",
        ),
    )
    assert handler.handle(request).status == "exception"
    assert not gateway.commands
    assert not gateway.triages


def test_lane_block_diagnose_core_admission_refusal_prevents_dispatch(harness):
    handler, gateway, _, reports, _ = harness
    gateway.refusal = "core refused subject kind or target_repo"
    assert handler.handle(blocked(reports)).status == "exception"
    assert not gateway.commands


def test_lane_block_diagnose_report_read_is_bounded_and_confined(harness, tmp_path):
    handler, gateway, _, reports, overlay = harness
    request = blocked(reports)
    outside = tmp_path / "outside.md"
    outside.write_text("repo-b/src/producer.py")
    terminal = request.terminal.model_copy(update={"report_path": str(outside)})
    assert (
        handler.handle(request.model_copy(update={"terminal": terminal})).status
        == "exception"
    )
    (reports / "lane-1.md").write_text("x" * (overlay.max_report_bytes + 1))
    assert handler.handle(request).status == "exception"
    assert not gateway.commands


@pytest.mark.parametrize(
    ("owner", "subject"), [("outside-map", "item"), ("repo-b", "pull_request")]
)
def test_lane_block_diagnose_two_core_refusal_fixture_rows(harness, owner, subject):
    """Prove the routing seam carries both rows to core and honors its refusal."""
    handler, gateway, _, _, _ = harness
    gateway.tickets["ticket-b"] = owner
    gateway.refusal = "core admission refused fixture row"
    request = ModelLaneRoutingRequest(
        operation="initial",
        item=ModelLaneRoutingItem(
            item_id="refused-item",
            text="A ticket item",
            ticket="ticket-b",
            subject_kind=subject,
        ),
    )
    assert handler.handle(request).status == "exception"
    (command,) = gateway.admissions
    assert command.target_repo == owner
    assert command.subject_kind == subject
    assert not gateway.commands


def test_lane_block_diagnose_triage_completion_releases_cap_and_does_not_reopen(
    harness,
):
    handler, gateway, store, _, _ = harness
    request = ModelLaneRoutingRequest(
        operation="initial", item=ModelLaneRoutingItem(item_id="new", text="An ask")
    )
    assert handler.handle(request).status == "triage"
    completed = request.model_copy(
        update={"triage_report": "Owner evidence: package_b.worker"}
    )
    assert handler.handle(completed).status == "dispatched"
    assert not store.read().triages
    assert handler.handle(completed).status == "joined"
    assert handler.handle(request).status == "joined"
    assert len(gateway.triages) == 1
    assert len(gateway.commands) == 1


def test_lane_block_diagnose_triage_none_queues_exception_and_releases_cap(harness):
    handler, gateway, store, _, _ = harness
    request = ModelLaneRoutingRequest(
        operation="initial", item=ModelLaneRoutingItem(item_id="new", text="An ask")
    )
    handler.handle(request)
    assert (
        handler.handle(
            request.model_copy(update={"triage_report": "none: no named evidence"})
        ).status
        == "exception"
    )
    assert not store.read().triages
    assert len(gateway.exceptions) == 1
    assert not gateway.commands


def test_lane_block_diagnose_concurrent_items_join_one_durable_claim(harness):
    from concurrent.futures import ThreadPoolExecutor

    handler, gateway, store, reports, overlay = harness
    other = HandlerLaneBlockDiagnoseEffect(
        overlay, gateway, HandlerLaneRoutingStore(Path(overlay.state_path))
    )
    requests = [
        blocked(reports, 1, item="first", cls="same"),
        blocked(reports, 2, item="second", cls="same"),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(effect.handle, request)
            for effect, request in zip((handler, other), requests, strict=True)
        ]
        results = [future.result() for future in futures]
    assert sorted(result.status for result in results) == ["dispatched", "joined"]
    (claim,) = store.read().claims.values()
    assert set(claim.item_ids) == {"first", "second"}
    assert gateway.bindings[claim.lane] == {"first", "second"}
    assert len(gateway.commands) == 1


@pytest.mark.parametrize(
    "profile", ["inmemory", pytest.param("redpanda", marks=pytest.mark.kafka)]
)
async def test_lane_block_diagnose_effect_runtime_profiles(
    harness, monkeypatch, tmp_path, profile
):
    """Exercise the contract adapter and real profile transport, with typed side-effect ports."""
    from omnimarket.nodes.node_lane_block_diagnose_effect.container import (
        container_lane_block_diagnose_effect as composition,
    )
    from tests.nodes.lane_closeout_support import run_over_profile

    _, gateway, _, reports, overlay = harness
    monkeypatch.setattr(composition, "build_dependencies", lambda: (overlay, gateway))
    requests = [blocked(reports, i) for i in range(7)]
    requests += [blocked(reports, 7, text="repo-a/src/producer.py")]
    requests += [blocked(reports, 8, text="Original error and reproducer are missing")]
    for expected in (
        ["dispatched"] * 7 + ["same-owner", "exception"],
        ["joined"] * 7 + ["same-owner", "exception"],
    ):
        for request, status in zip(requests, expected, strict=True):
            runtime = await run_over_profile(
                "node_lane_block_diagnose_effect",
                request,
                profile,
                tmp_path,
                monkeypatch,
            )
            assert runtime.handler_result.status == status
    assert len(gateway.commands) == 7
    assert len(gateway.exceptions) == 1


def test_lane_block_diagnose_deployment_overlay_and_metadata():
    import importlib.metadata

    import yaml

    from omnimarket.nodes.node_lane_block_diagnose_effect.models.model_routing_bindings import (
        ModelLaneRoutingDeployment,
    )
    from scripts.ci.check_node_metadata_dependencies import (
        SHARED_RUNTIME_DEPENDENCIES,
        imported_distributions,
        load_node_dependencies,
    )

    root = Path(__file__).resolve().parents[2] / "src/omnimarket/nodes"
    effect = root / "node_lane_block_diagnose_effect"
    deployment = ModelLaneRoutingDeployment.model_validate(
        yaml.safe_load((effect / "overlay.example.yaml").read_text())
    )
    contract = yaml.safe_load((effect / "contract.yaml").read_text())
    for name in (
        "diagnosis",
        "admission",
        "fill",
        "bind",
        "exception",
        "ticket",
        "triage",
    ):
        assert (
            getattr(deployment.bindings, name).command_topic
            in contract["event_bus"]["publish_topics"]
        )
    entries = importlib.metadata.entry_points(group="onex.nodes")
    for name in ("node_lane_block_diagnose_compute", "node_lane_block_diagnose_effect"):
        assert len(entries.select(name=name)) == 1
        assert not (
            imported_distributions(root / name)
            - set(SHARED_RUNTIME_DEPENDENCIES)
            - load_node_dependencies(root / name / "metadata.yaml")
        )


def test_lane_block_diagnose_bus_gateway_preserves_typed_seams(harness):
    import yaml
    from pydantic import TypeAdapter

    from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
    from omnimarket.models.exception_queue import ModelExceptionEmitRequest
    from omnimarket.nodes.node_lane_block_diagnose_effect.adapters.adapter_routing_gateway import (
        HandlerLaneRoutingGateway,
    )
    from omnimarket.nodes.node_lane_block_diagnose_effect.models.model_routing_bindings import (
        ModelLaneRoutingDeployment,
    )

    _, fake, _, reports, overlay = harness
    path = (
        Path(__file__).resolve().parents[2]
        / "src/omnimarket/nodes/node_lane_block_diagnose_effect/overlay.example.yaml"
    )
    deployment = ModelLaneRoutingDeployment.model_validate(
        yaml.safe_load(path.read_text())
    )
    ports = deployment.bindings
    requests = []
    admission_reply = {"allowed": True, "reason": "fixture core receipt"}

    def publish(**kwargs):
        requests.append(kwargs)
        topic, payload = kwargs["command_topic"], kwargs["payload"]
        if topic == ports.diagnosis.command_topic:
            return {
                "payload": fake.diagnose(
                    ModelLaneBlockFacts.model_validate(payload)
                ).model_dump()
            }
        if topic == ports.admission.command_topic:
            return {"payload": admission_reply}
        if topic == ports.fill.command_topic:
            return {"payload": {"lane": "owner-lane"}}
        if topic == ports.bind.command_topic:
            return {"payload": {"item_ids": payload["item_ids"]}}
        if topic == ports.exception.command_topic:
            TypeAdapter(ModelExceptionEmitRequest).validate_python(payload)
            return {"payload": {"status": "appended"}}
        if topic == ports.ticket.command_topic:
            return {"payload": {"repository": "repo-b"}}
        if topic == ports.triage.command_topic:
            assert payload["allow_code"] is False
            assert payload["worker"] is False
            return {"payload": {"lane": "triage-lane"}}
        raise AssertionError(topic)

    gateway = HandlerLaneRoutingGateway(
        ports, publish=publish, broker=lambda _: ModelNodeBroker(kind="inmemory")
    )
    handler = HandlerLaneBlockDiagnoseEffect(overlay, gateway)
    assert handler.handle(blocked(reports)).lane == "owner-lane"
    (send,) = [
        request["payload"]
        for request in requests
        if request["command_topic"] == ports.fill.command_topic
    ]
    assert send["target_repo"] == "repo-b"
    assert send["report_text"] in send["brief"]
    assert send["for_items"] == ["item-1"]
    admission_reply.update(allowed=False, reason="core refusal")
    assert handler.handle(blocked(reports, 2)).status == "exception"
    assert (
        len([r for r in requests if r["command_topic"] == ports.fill.command_topic])
        == 1
    )
    assert gateway.ticket_repository("ticket-b") == "repo-b"
    initial = ModelLaneRoutingRequest(
        operation="initial",
        item=ModelLaneRoutingItem(item_id="triage-item", text="an ask"),
    )
    assert handler.handle(initial).lane == "triage-lane"
    assert {request["command_topic"] for request in requests} == {
        "onex.cmd.omnimarket.lane-block-owner-requested.v1",
        "onex.cmd.omnimarket.fill-backlog-admission-requested.v1",
        "onex.cmd.omnimarket.fill-backlog-item-requested.v1",
        "onex.cmd.omnimarket.item-lane-bind-requested.v1",
        "onex.cmd.omnimarket.exception-emit-requested.v1",
        "onex.cmd.omnimarket.ticket-repository-requested.v1",
        "onex.cmd.omnimarket.item-owner-triage-requested.v1",
    }


async def test_lane_block_diagnose_installed_contracts_auto_wire(harness, monkeypatch):
    import asyncio
    import json
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.nodes.node_lane_block_diagnose_effect.container import (
        container_lane_block_diagnose_effect as composition,
    )

    _, gateway, _, reports, overlay = harness
    monkeypatch.setattr(composition, "build_dependencies", lambda: (overlay, gateway))
    names = {"node_lane_block_diagnose_compute", "node_lane_block_diagnose_effect"}
    discovered = tuple(
        contract
        for contract in discover_contracts().contracts
        if contract.name in names
    )
    assert {contract.name for contract in discovered} == names
    root = Path(__file__).resolve().parents[2] / "src/omnimarket/nodes"
    contracts = {
        name: yaml.safe_load((root / name / "contract.yaml").read_text())
        for name in names
    }
    bus = EventBusInmemory(environment="local", group="lane-owner-wiring")
    await bus.start()
    events = asyncio.Queue()

    async def collect(message):
        await events.put((message.topic, json.loads(message.value)))

    try:
        for contract in contracts.values():
            await bus.subscribe(
                contract["terminal_event"],
                group_id="lane-owner-results",
                on_message=collect,
            )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=discovered),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert {
            result.contract_name: result.outcome.value for result in report.results
        } == dict.fromkeys(names, "wired")
        engine.freeze()
        for name, payload, expected in (
            (
                "node_lane_block_diagnose_compute",
                ModelLaneBlockFacts(
                    blocker="repo-b/src/producer.py",
                    report_text="",
                    owner_map=overlay.owner_map,
                ),
                "owner",
            ),
            ("node_lane_block_diagnose_effect", blocked(reports), "status"),
        ):
            correlation = uuid4()
            envelope = ModelEventEnvelope[object](
                payload=payload.model_dump(mode="json"), correlation_id=correlation
            )
            await bus.publish(
                contracts[name]["runtime_dispatch"]["command_topic"],
                None,
                envelope.model_dump_json().encode(),
                None,
            )
            topic, event = await asyncio.wait_for(events.get(), timeout=5)
            assert (
                topic
                == {
                    "node_lane_block_diagnose_compute": "onex.evt.omnimarket.lane-block-owner-completed.v1",
                    "node_lane_block_diagnose_effect": "onex.evt.omnimarket.lane-block-diagnose-completed.v1",
                }[name]
            )
            assert event["correlation_id"] == str(correlation)
            assert event["payload"][expected] == (
                "repo-b" if expected == "owner" else "dispatched"
            )
    finally:
        await bus.close()


def test_lane_block_diagnose_constructs_without_deployment_overlay(monkeypatch):
    """A clean runtime must wire before an operator supplies routing deployment facts."""
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError

    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    handler = HandlerLaneBlockDiagnoseEffect()
    request = ModelLaneRoutingRequest(
        operation="initial",
        item=ModelLaneRoutingItem(item_id="unconfigured", text="ask"),
    )
    with pytest.raises(NodeOverlayError, match="node_lane_block_diagnose_effect"):
        handler.handle(request)
