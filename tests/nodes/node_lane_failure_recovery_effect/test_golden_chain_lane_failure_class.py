# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane receipts keep the engine kill separate from retention failures."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorEvent,
    ModelErrorNormalizerFacts,
)
from omnimarket.models.model_lane_failure import (
    ModelLaneFailureAdmission,
    ModelLaneFailureAdmissionRequest,
    ModelLaneFailureOverlay,
    ModelLaneFailureOwnerReply,
    ModelLaneFailureOwnerRequest,
    ModelLaneFailureReceipt,
    ModelLaneFailureRequest,
    ModelLaneMemoryStep,
    ModelLaneRecoveryAck,
    ModelLaneRecoveryContribution,
)
from omnimarket.nodes.node_error_class_normalize_compute.handlers.handler_error_class_normalize import (
    HandlerErrorClassNormalize,
)
from omnimarket.nodes.node_lane_failure_recovery_effect.handlers.handler_lane_failure_recovery import (
    HandlerLaneFailureRecovery,
)

ROOT = Path(__file__).parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_lane_failure_recovery_effect"


@pytest.mark.asyncio
async def test_lane_failure_runtime_constructs_before_overlay_is_available(
    monkeypatch, b7_receipts
) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError
    from omnimarket.nodes.node_lane_failure_recovery_effect.handlers.handler_lane_retention_verify import (
        HandlerLaneRetentionVerify,
    )

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", "")
    recovery = HandlerLaneFailureRecovery()
    verification = HandlerLaneRetentionVerify()
    assert callable(verification.handle)
    with pytest.raises(NodeOverlayError, match="lane_failure_recovery"):
        await recovery.handle(failure_request(b7_receipts[0]))


@pytest.fixture
def failure_overlay() -> ModelLaneFailureOverlay:
    return ModelLaneFailureOverlay.model_validate(
        yaml.safe_load((NODE / "overlay.example.yaml").read_text())
    )


@pytest.fixture
def b7_receipts() -> tuple[ModelLaneFailureReceipt, ...]:
    document = json.loads(
        (ROOT / "tests/fixtures/lane_failure/b7_receipt_extracts.json").read_text()
    )
    return tuple(
        ModelLaneFailureReceipt.model_validate(row) for row in document["receipts"]
    )


class SharedPlanFixture:
    """The shared claim boundary, injected in place of the external dispatcher.

    Claims live in the service fixture, outside handler instances. These tests
    count claimed starts, not live lab lanes, and do not assert its deployment.
    """

    def __init__(self, overlay: ModelLaneFailureOverlay) -> None:
        self.overlay = overlay
        self.mode = "dispatch"
        self.allow = True
        self.claims: dict[str, ModelLaneRecoveryContribution] = {}
        self.origins: dict[str, set[str]] = {}
        self.starts: list[ModelLaneRecoveryContribution] = []
        self.escalations: list[ModelLaneRecoveryContribution] = []
        self.owner_calls: list[ModelLaneFailureOwnerRequest] = []
        self.admissions: list[ModelLaneFailureAdmissionRequest] = []
        self.owner_repo: str | None = "runner_tools"
        self.lose_next_reply = False
        self.lock = asyncio.Lock()

    async def normalize(self, request: ModelErrorClassNormalizeRequest):
        return HandlerErrorClassNormalize().handle(request)

    async def admit(
        self, request: ModelLaneFailureAdmissionRequest
    ) -> ModelLaneFailureAdmission:
        self.admissions.append(request)
        # Core is a separate authority, stubbed by an explicit admitted decision.
        # The handler must transmit the complete table rather than validate it.
        assert request.document == self.overlay.error_rule_document
        rows = request.document["error_rules"]
        assert isinstance(rows, list)
        row = next(
            item
            for item in rows
            if isinstance(item, dict) and item["id"] == request.rule_id
        )
        assert request.class_key in row["class_keys"]
        assert request.key_version == row["key_version"]
        assert request.normalizer_key_version == self.overlay.normalizer_key_version
        return ModelLaneFailureAdmission(
            allowed=self.allow,
            rule_id=request.rule_id,
            class_key=request.class_key,
            mode=self.mode,
            action="fill-backlog-item",
            reason="test admission fixture",
        )

    async def owner(
        self, request: ModelLaneFailureOwnerRequest
    ) -> ModelLaneFailureOwnerReply:
        self.owner_calls.append(request)
        assert request.owner_map == self.overlay.owner_map
        return ModelLaneFailureOwnerReply(
            owner=self.owner_repo, reason="test owner-map reply"
        )

    async def contribute(
        self, request: ModelLaneRecoveryContribution
    ) -> ModelLaneRecoveryAck:
        async with self.lock:
            self.origins.setdefault(request.key, set()).update(request.for_items)
            first = request.key not in self.claims
            if first:
                self.claims[request.key] = request
                (
                    self.escalations if request.operation == "escalate" else self.starts
                ).append(request)
            if self.lose_next_reply:
                self.lose_next_reply = False
                raise TimeoutError("test lost reply after claim")
            return ModelLaneRecoveryAck(
                key=request.key,
                status="escalated"
                if first and request.operation == "escalate"
                else "claimed"
                if first
                else "joined",
                lane="test-replacement" if request.operation != "escalate" else None,
            )


def failure_request(
    receipt: ModelLaneFailureReceipt, **updates: Any
) -> ModelLaneFailureRequest:
    return ModelLaneFailureRequest.model_validate(
        {
            "receipt": receipt,
            "attempt_family": "attempt-family:" + receipt.lane,
            "original_lane": receipt.lane,
            "original_brief": "test original brief; preserve its bytes\n",
            "for_items": ["test-item:" + receipt.lane],
            "target_repository": "application_tools",
            **updates,
        }
    )


def test_lane_failure_class_has_one_runner_producer() -> None:
    """The lane name is a subject, never the producer's class identity."""
    root = Path(__file__).parents[3]
    configuration = yaml.safe_load(
        (
            root
            / "src/omnimarket/nodes/node_error_class_normalize_compute/overlay.example.yaml"
        ).read_text()
    )
    adapters = [
        row
        for row in configuration["producer_adapters"]
        if row["source"] == "lane" and row["producer"] == "remote-lane-runner"
    ]
    assert len(adapters) == 1
    assert adapters[0]["unique_codes"] is True
    facts = ModelErrorNormalizerFacts(configuration=configuration, hosts=())
    keys = {
        HandlerErrorClassNormalize()
        .handle(
            ModelErrorClassNormalizeRequest(
                event=ModelErrorEvent(
                    source="lane",
                    producer="remote-lane-runner",
                    code="lane-oom",
                    text="scope_result=oom-kill",
                    subject={"lane": lane},
                ),
                facts=facts,
            )
        )
        .class_key
        for lane in ("pfix-68-1316", "lane-push-race-1316")
    }
    assert len(keys) == 1


@pytest.mark.asyncio
async def test_lane_failure_class_b7_two_signals_and_shared_retention(
    failure_overlay,
    b7_receipts,
) -> None:
    shared = SharedPlanFixture(failure_overlay)
    results = []
    for receipt in b7_receipts:
        result = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
            failure_request(receipt)
        )
        results.append({match.rule_id: match for match in result.matches})
        assert set(results[-1]) == {"lane-oom", "lane-retention-import"}
    oom_key = results[0]["lane-oom"].class_key
    retention_key = results[0]["lane-retention-import"].class_key
    assert oom_key == results[1]["lane-oom"].class_key
    assert retention_key == results[1]["lane-retention-import"].class_key
    assert oom_key != retention_key
    assert len([start for start in shared.starts if start.operation == "replace"]) == 2
    assert (
        len([start for start in shared.starts if start.operation == "runner-fix"]) == 1
    )
    fix = next(start for start in shared.starts if start.operation == "runner-fix")
    assert fix.target_repo == "runner_tools"
    assert len(shared.origins[fix.key]) == 2
    assert all(
        call.blocker == failure_overlay.runner_path for call in shared.owner_calls
    )


@pytest.mark.asyncio
async def test_lane_failure_class_argument_limit_is_distinct(
    failure_overlay, b7_receipts
) -> None:
    shared = SharedPlanFixture(failure_overlay)
    first = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
        failure_request(b7_receipts[0])
    )
    receipt = ModelLaneFailureReceipt(
        lane="test-arglimit",
        run_id="test-arglimit",
        retention={
            "rc": 1,
            "reading": "OSError: [Errno 7] Argument list too long: '/usr/bin/ssh'",
        },
    )
    second = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
        failure_request(receipt)
    )
    assert len(second.matches) == 1
    assert second.matches[0].rule_id == "lane-retention-argument-limit"
    assert second.matches[0].class_key not in {
        match.class_key for match in first.matches
    }
    assert (
        len([start for start in shared.starts if start.operation == "runner-fix"]) == 2
    )


@pytest.mark.asyncio
async def test_lane_failure_class_claim_count_replays_and_second_oom(
    failure_overlay, b7_receipts
) -> None:
    shared = SharedPlanFixture(failure_overlay)
    request = failure_request(b7_receipts[0], target_repository="different_repository")
    # Several handler instances are redelivery, never another dispatcher.
    await asyncio.gather(
        *(
            HandlerLaneFailureRecovery(failure_overlay, shared).handle(request)
            for _ in range(5)
        )
    )
    replacements = [start for start in shared.starts if start.operation == "replace"]
    assert len(replacements) == 1
    replacement = replacements[0]
    assert replacement.attempt_family == request.attempt_family
    assert replacement.target_repo == "different_repository"
    assert replacement.brief == request.original_brief
    assert replacement.mem_max == "16G"
    assert replacement.mem_bytes == 17179869184
    second = failure_request(
        b7_receipts[1],
        attempt_family=request.attempt_family,
        original_lane=request.original_lane,
        replacement_index=1,
    )
    await HandlerLaneFailureRecovery(failure_overlay, shared).handle(second)
    await HandlerLaneFailureRecovery(failure_overlay, shared).handle(second)
    assert len(shared.escalations) == 1
    assert len([start for start in shared.starts if start.operation == "replace"]) == 1


@pytest.mark.asyncio
async def test_lane_failure_class_lost_reply_keeps_other_signal(
    failure_overlay, b7_receipts
) -> None:
    shared = SharedPlanFixture(failure_overlay)
    shared.lose_next_reply = True
    request = failure_request(b7_receipts[0])
    result = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(request)
    assert [match.status for match in result.matches] == ["pending", "claimed"]
    assert result.matches[0].contribution is not None
    replay = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(request)
    assert [match.status for match in replay.matches] == ["joined", "joined"]
    assert len(shared.starts) == 2


@pytest.mark.asyncio
async def test_lane_failure_class_memory_is_overlay_data_and_salvage_is_evidence(
    failure_overlay, b7_receipts
) -> None:
    overlay = failure_overlay.model_copy(
        update={
            "memory_steps": (ModelLaneMemoryStep(mem_bytes=25769803776, mem_max="24G"),)
        }
    )
    shared = SharedPlanFixture(overlay)
    raw = b7_receipts[0].model_dump()
    raw.update(
        workdir={"tree": "/test/kept-tree", "disposition": "kept"}, host="test-host"
    )
    receipt = ModelLaneFailureReceipt.model_validate(raw)
    result = await HandlerLaneFailureRecovery(overlay, shared).handle(
        failure_request(receipt)
    )
    command = result.matches[0].contribution
    assert command.mem_max == "24G"
    assert command.salvage_worktree == "/test/kept-tree"
    assert command.salvage_host == "test-host"
    assert command.evidence == receipt
    original = await HandlerLaneFailureRecovery(
        failure_overlay, SharedPlanFixture(failure_overlay)
    ).handle(failure_request(b7_receipts[0]))
    assert original.matches[0].contribution.salvage_worktree is None


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [0, 34359738368])
async def test_lane_failure_class_missing_or_max_memory_escalates(
    failure_overlay, b7_receipts, current
) -> None:
    shared = SharedPlanFixture(failure_overlay)
    raw = b7_receipts[0].model_dump()
    raw["guard"]["requested"]["mem_bytes"] = current
    receipt = ModelLaneFailureReceipt.model_validate(raw)
    await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
        failure_request(receipt)
    )
    assert len(shared.escalations) == 1
    assert not [start for start in shared.starts if start.operation == "replace"]


@pytest.mark.asyncio
async def test_lane_failure_class_exit_137_is_not_a_guard_kill(failure_overlay) -> None:
    shared = SharedPlanFixture(failure_overlay)
    receipt = ModelLaneFailureReceipt(
        lane="test-other-exit", run_id="test-other-exit", engine_rc=137
    )
    result = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
        failure_request(receipt)
    )
    assert not result.matches
    assert not shared.starts
    assert not shared.admissions


def test_lane_failure_class_table_and_peer_contracts_are_bound(failure_overlay) -> None:
    assert failure_overlay.error_rule_document == yaml.safe_load(
        (NODE / "lane_failure.error_rules.yaml").read_text()
    )
    owner = failure_overlay.bindings.owner
    # Public contract of the existing ownership node; no sibling-clone dependency.
    assert owner.command_topic == "onex.cmd.omnimarket.lane-block-owner-requested.v1"
    assert owner.terminal_topic == "onex.evt.omnimarket.lane-block-owner-completed.v1"
    assert failure_overlay.rule_key_version == 1
    assert len(failure_overlay.normalizer_key_version) == 64


@pytest.mark.asyncio
async def test_lane_failure_class_gateway_uses_declared_node_transport(
    failure_overlay,
) -> None:
    from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
    from omnimarket.nodes.node_lane_failure_recovery_effect.container.gateway_lane_failure import (
        LaneFailureGateway,
    )

    observed = []

    def publish(**kwargs):
        observed.append(kwargs)
        return {
            "payload": {
                "owner": "configured_runner_owner",
                "reason": "test transport reply",
            }
        }

    nodes = []

    def broker(node):
        nodes.append(node)
        return ModelNodeBroker(
            kind="kafka", bootstrap_servers="test-declared-broker:12345"
        )

    gateway = LaneFailureGateway(
        failure_overlay.bindings, publish=publish, broker=broker
    )
    request = ModelLaneFailureOwnerRequest(
        blocker="test/runner.py", report_text="test report", owner_map={"rules": []}
    )
    result = await gateway.owner(request)
    assert result.owner == "configured_runner_owner"
    endpoint = failure_overlay.bindings.owner
    assert nodes == [endpoint.node]
    assert observed[0]["node_id"] == endpoint.node
    assert observed[0]["command_topic"] == endpoint.command_topic
    assert observed[0]["response_topic"] == endpoint.terminal_topic
    assert observed[0]["bootstrap_servers"] == "test-declared-broker:12345"
    assert observed[0]["payload"] == request.model_dump(mode="json")
    unanswered = LaneFailureGateway(
        failure_overlay.bindings, publish=lambda **_kwargs: None, broker=broker
    )
    with pytest.raises(TimeoutError, match="replay with the same key"):
        await unanswered.owner(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "hint"])
async def test_lane_failure_class_core_owns_admission_and_mode(
    failure_overlay, b7_receipts, mode
) -> None:
    shared = SharedPlanFixture(failure_overlay)
    shared.mode = mode
    result = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
        failure_request(b7_receipts[0])
    )
    assert [match.status for match in result.matches] == [mode, mode]
    assert not shared.starts
    shared.allow = False
    result = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
        failure_request(b7_receipts[0])
    )
    assert all(match.status == "refused" for match in result.matches)
    assert not shared.starts


@pytest.mark.asyncio
async def test_lane_failure_class_unresolved_owner_sends_no_runner_lane(
    failure_overlay, b7_receipts
) -> None:
    shared = SharedPlanFixture(failure_overlay)
    shared.owner_repo = None
    result = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
        failure_request(b7_receipts[0])
    )
    assert result.matches[1].status == "refused"
    assert [start.operation for start in shared.starts] == ["replace"]


@pytest.mark.asyncio
async def test_lane_failure_class_retention_owner_is_runner_not_missing_package(
    failure_overlay,
    b7_receipts,
) -> None:
    shared = SharedPlanFixture(failure_overlay)
    result = await HandlerLaneFailureRecovery(failure_overlay, shared).handle(
        failure_request(b7_receipts[0])
    )
    owner_request = shared.owner_calls[0]
    # The 6.5 owner map includes package names as evidence. A failed import is
    # not evidence that its missing package owns the runner's retention step.
    assert "No module named 'omnimarket'" not in owner_request.report_text
    assert owner_request.blocker == failure_overlay.runner_path
    assert (
        result.matches[1].contribution.evidence.retention.error
        == b7_receipts[0].retention.error
    )


@pytest.mark.parametrize("checkpoint", [3600, 86400])
def test_lane_failure_class_retention_scan_uses_start_time_and_install_record(
    tmp_path, checkpoint
) -> None:
    from datetime import UTC, datetime, timedelta

    from omnimarket.models.model_lane_failure import (
        ModelLaneRetentionVerifyOverlay,
        ModelLaneRetentionVerifyRequest,
    )
    from omnimarket.nodes.node_lane_failure_recovery_effect.handlers.handler_lane_retention_verify import (
        HandlerLaneRetentionVerify,
    )

    installed = datetime(2026, 11, 4, 12, tzinfo=UTC)
    record = tmp_path / "installed.json"
    record.write_text(
        json.dumps(
            {
                "installed_at": installed.isoformat(),
                "host": "test-host",
                "revision": "test-installed-revision",
            }
        )
    )
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    request = ModelLaneRetentionVerifyRequest(
        receipt_directory=str(receipts),
        install_record=str(record),
        now=installed + timedelta(seconds=checkpoint),
        check_after_seconds=checkpoint,
    )
    overlay = ModelLaneRetentionVerifyOverlay.model_validate(
        yaml.safe_load((NODE / "retention_verify.overlay.example.yaml").read_text())
    )
    handler = HandlerLaneRetentionVerify(overlay)
    assert handler.handle(request).status == "unmeasured"
    before = {
        "lane": "test-before",
        "run_id": "test-before",
        "host": "test-host",
        "started_at": (installed - timedelta(seconds=1)).isoformat(),
        "retention": {"error": "ModuleNotFoundError: No module named 'omnimarket'"},
    }
    (receipts / "before.json").write_text(json.dumps(before))
    assert handler.handle(request).status == "unmeasured"
    after = {
        "lane": "test-after",
        "run_id": "test-after",
        "host": "test-host",
        "started_at": (installed + timedelta(seconds=1)).isoformat(),
    }
    target = receipts / "after.json"
    target.write_text(json.dumps(after))
    result = handler.handle(request)
    assert result.status == "clear"
    assert result.checked_receipts == 1
    after["retention"] = before["retention"]
    target.write_text(json.dumps(after))
    result = handler.handle(request)
    assert result.status == "failed"
    assert result.violating_receipts == (str(target),)
    assert result.installed.revision == "test-installed-revision"
    assert (
        handler.handle(request.model_copy(update={"now": installed})).status
        == "not-due"
    )
    (receipts / "broken.json").write_text("{")
    target.write_text(json.dumps({**after, "retention": {}}))
    result = handler.handle(request)
    assert result.status == "unmeasured"
    assert result.unreadable_receipts == (str(receipts / "broken.json"),)


@pytest.mark.asyncio
async def test_lane_failure_class_runtime_bus_profile(
    failure_overlay, b7_receipts
) -> None:
    """All four peer boundaries and both B7 extracts use the selected runtime bus.

    The admission/claim services are fixtures: no live replacement is claimed.
    Selected Redpanda connectivity/credential failures fail, never skip.
    """
    import importlib
    import os
    from contextlib import nullcontext
    from uuid import uuid4

    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    from omnimarket.models.model_lane_failure import (
        ModelLaneFailureEndpoint,
        ModelLaneFailureResult,
    )
    from omnimarket.nodes.node_lane_failure_recovery_effect.container.gateway_lane_failure import (
        LaneFailureGateway,
    )
    from tests.runtime_local_compat import LocalRuntimeBusAdapter

    selected = os.environ.get("ONEX_LANE_FAILURE_PROFILE", "inmemory")
    profile = yaml.safe_load((NODE / f"overlay.{selected}.yaml").read_text())[
        "event_bus"
    ]
    binding = nullcontext()
    if profile["type"] == "kafka":
        from omnibase_infra.cli.delegate_lane import resolve_lane_selection_at
        from omnibase_infra.cli.delegate_lane_credentials import (
            resolve_lane_client_transport_for,
        )
        from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
        from omnibase_infra.event_bus.lane_client_transport_binding import (
            bind_lane_client_transport,
        )

        from tests.bus_lane_guard import (
            assert_broker_in_declared_lane,
            declared_test_lane,
            resolve_test_bus_broker,
        )

        assert profile["lane"] == declared_test_lane()
        broker = resolve_test_bus_broker()
        assert_broker_in_declared_lane(broker)
        selection = resolve_lane_selection_at(declared_test_lane(), ROOT)
        assert selection.bootstrap_servers == broker
        transport = resolve_lane_client_transport_for(
            lane_target=selection, onex_home=Path.home() / ".onex", environ=os.environ
        )
        binding = (
            bind_lane_client_transport(transport)
            if transport is not None
            else nullcontext()
        )
        with binding:
            bus = EventBusKafka.from_bootstrap(broker)
    else:
        assert profile["type"] == "inmemory"
        bus = EventBusInmemory(
            environment="lane-failure-proof", group="lane-failure-proof"
        )
    suffix = uuid4().hex

    def isolated(endpoint):
        return endpoint.model_copy(
            update={
                "command_topic": endpoint.command_topic.removesuffix(".v1")
                + f".{suffix}.v1",
                "terminal_topic": endpoint.terminal_topic.removesuffix(".v1")
                + f".{suffix}.v1",
            }
        )

    bindings = failure_overlay.bindings.model_copy(
        update={
            name: isolated(getattr(failure_overlay.bindings, name))
            for name in ("normalizer", "owner", "admission", "recovery")
        }
    )
    shared = SharedPlanFixture(failure_overlay)

    class MethodHandler:
        def __init__(self, method):
            self.method = method

        async def handle(self, request):
            return await self.method(request)

    queues: dict[str, asyncio.Queue] = {}
    observed_topics: set[str] = set()
    stops = []
    errors = []

    async def attach(endpoint, handler, model):
        queue = asyncio.Queue()
        queues[endpoint.terminal_topic] = queue

        async def observe(message):
            observed_topics.add(endpoint.terminal_topic)
            await queue.put(json.loads(message.value))

        adapter = LocalRuntimeBusAdapter(
            handler=handler,
            handler_name=type(handler).__name__,
            input_model_cls=model,
            output_topic=endpoint.terminal_topic,
            bus=bus,
            on_error=lambda: errors.append(endpoint.node),
        )
        stops.append(await bus.subscribe_ungrouped(endpoint.terminal_topic, observe))
        stops.append(
            await bus.subscribe_ungrouped(endpoint.command_topic, adapter.on_message)
        )

    async def call(endpoint, payload):
        observed_topics.add(endpoint.command_topic)
        await bus.publish(endpoint.command_topic, None, json.dumps(payload).encode())
        return await asyncio.wait_for(queues[endpoint.terminal_topic].get(), timeout=30)

    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    spec = contract["handler"]
    handler_cls = getattr(importlib.import_module(spec["module"]), spec["class"])
    gateway = LaneFailureGateway(bindings, call=call)
    endpoint = isolated(
        ModelLaneFailureEndpoint(
            node=contract["name"],
            command_topic=contract["runtime_dispatch"]["command_topic"],
            terminal_topic=contract["terminal_event"],
        )
    )
    actual = []
    await bus.start()
    try:
        await attach(
            bindings.normalizer,
            MethodHandler(shared.normalize),
            ModelErrorClassNormalizeRequest,
        )
        await attach(
            bindings.owner, MethodHandler(shared.owner), ModelLaneFailureOwnerRequest
        )
        await attach(
            bindings.admission,
            MethodHandler(shared.admit),
            ModelLaneFailureAdmissionRequest,
        )
        await attach(
            bindings.recovery,
            MethodHandler(shared.contribute),
            ModelLaneRecoveryContribution,
        )
        await attach(
            endpoint, handler_cls(failure_overlay, gateway), ModelLaneFailureRequest
        )
        for receipt in b7_receipts:
            result = ModelLaneFailureResult.model_validate(
                await call(endpoint, failure_request(receipt).model_dump(mode="json"))
            )
            actual.append(result)
        assert not errors
        assert {
            "onex.evt.omnimarket.lane-failure-recovered.v1",
            "onex.evt.omnimarket.error-class-normalized.v1",
            "onex.cmd.omnimarket.error-class-normalize.v1",
            "onex.cmd.omnimarket.lane-block-owner-requested.v1",
            "onex.cmd.omnimarket.error-rule-admission.v1",
            "onex.cmd.omnimarket.recovery-plan-requested.v1",
        } <= {topic.replace(f".{suffix}.v1", ".v1") for topic in observed_topics}
        assert actual[0].matches[0].class_key == actual[1].matches[0].class_key
        assert actual[0].matches[1].class_key == actual[1].matches[1].class_key
        assert actual[0].matches[0].class_key != actual[0].matches[1].class_key
        assert (
            len([start for start in shared.starts if start.operation == "replace"]) == 2
        )
        assert (
            len([start for start in shared.starts if start.operation == "runner-fix"])
            == 1
        )
        # Direct compute and runtime adapter must agree byte for byte.
        direct_service = SharedPlanFixture(failure_overlay)
        expected = []
        for receipt in b7_receipts:
            expected.append(
                await handler_cls(failure_overlay, direct_service).handle(
                    failure_request(receipt)
                )
            )
        assert [result.model_dump_json() for result in actual] == [
            result.model_dump_json() for result in expected
        ]
        target = os.environ.get("ONEX_LANE_FAILURE_KEYS_FILE")
        if target:
            Path(target).write_text(
                json.dumps(
                    [
                        [
                            (match.class_key, match.key_version)
                            for match in result.matches
                        ]
                        for result in actual
                    ]
                )
            )
    finally:
        for stop in reversed(stops):
            await stop()
        await bus.close()
