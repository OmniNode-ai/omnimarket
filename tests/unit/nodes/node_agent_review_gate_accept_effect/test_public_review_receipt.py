# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded placement regression: public reviews occupied lab Actions runners.

The hosted job must only consume a signed verdict; voting is a bus workload.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_agent_review_gate_accept_effect.handlers.handler_gate_accept import (
    HandlerGateAccept,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptDecision as Decision,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import (
    load_gate_accept_policy,
)

from .support import (
    HEAD,
    OTHER_HEAD,
    OUTPUT_PATH,
    PR,
    REPO,
    SUMMARY_PATH,
    FakeFiles,
    FakeReviews,
    FakeVerifier,
    accepted,
    receipt_review,
    refused,
)

ROOT = Path(__file__).resolve().parents[4]


def receipt_request():
    from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
        model_gate_accept,
    )

    assert hasattr(model_gate_accept, "ModelReceiptGateRequest"), (
        "a hosted gate needs a typed receipt-only request with no voter result file"
    )
    return model_gate_accept.ModelReceiptGateRequest(
        repository=REPO,
        pull_request=PR,
        head_sha=HEAD,
        step_output_path=OUTPUT_PATH,
        step_summary_path=SUMMARY_PATH,
    )


def test_receipt_only_accepts_verified_exact_head_without_running_voters() -> None:
    verifier = FakeVerifier({"signed": accepted("signed")})
    files = FakeFiles()
    gate = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([receipt_review("signed")]),
        verifier=verifier,
        files=files,
    )
    request = receipt_request()
    result = gate.handle_receipt(request)
    assert result.decision is Decision.PASS_AGENT
    assert result.receipt_id == "signed"
    assert verifier.calls[0].head_sha == HEAD
    assert "decision=pass_agent" in files.files[OUTPUT_PATH]


@pytest.mark.parametrize(
    "reason",
    [
        "signature_invalid",
        "head_differs",
        "quorum_insufficient",
        "publisher_forbidden",
        "blocked",
    ],
)
def test_receipt_only_keeps_verifier_refusals_red(reason: str) -> None:
    gate = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([receipt_review("bad")]),
        verifier=FakeVerifier({"bad": refused(reason)}),
        files=FakeFiles(),
    )
    result = gate.handle_receipt(receipt_request())
    assert result.decision is Decision.REFUSE
    assert reason in result.summary_line


@pytest.mark.parametrize(
    "review",
    [
        receipt_review("wrong-login", author="other[bot]"),
        receipt_review("old", commit_id=OTHER_HEAD),
        receipt_review("dismissed", state="DISMISSED"),
    ],
)
def test_receipt_only_ignores_other_login_head_or_dismissed_review(review) -> None:
    verifier = FakeVerifier()
    gate = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([review]),
        verifier=verifier,
        files=FakeFiles(),
    )
    result = gate.handle_receipt(receipt_request())
    assert result.decision is not Decision.PASS_AGENT
    assert verifier.calls == []


def test_receipt_only_refuses_unreadable_reviews() -> None:
    gate = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([], fail=True),
        verifier=FakeVerifier(),
        files=FakeFiles(),
    )
    result = gate.handle_receipt(receipt_request())
    assert result.decision is Decision.REFUSE


def test_pr_state_has_proactive_review_route_without_actions_failure() -> None:
    contract = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_agent_review_dispatch_orchestrator/contract.yaml"
        ).read_text()
    )
    routes = contract["handler_routing"]["handlers"]
    assert any(route["operation"] == "request_pr_review" for route in routes), (
        "receipt-only Actions cannot depend on a producer that waits for failed CI voters"
    )


async def test_pr_observation_publishes_once_with_same_identity_as_fallback(
    tmp_path,
) -> None:
    import hashlib

    from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers.handler_pr_review_trigger import (
        HandlerPrReviewTrigger,
    )
    from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models.model_pr_review_trigger import (
        ModelPrReviewObservation,
        ModelPrReviewTriggerConfig,
    )

    class Bus:
        def __init__(self):
            self.commands = []
            self.head = HEAD
            self.state = "OPEN"
            self.base = "dev"
            self.fail = False
            self.calls = []

        async def call(self, node, request):
            self.calls.append(request)
            return {
                "repo": REPO,
                "pr_number": PR,
                "title": "",
                "body": "",
                "body_truncated": False,
                "state": self.state,
                "is_draft": False,
                "merged": False,
                "author": "author",
                "head_ref": "feature",
                "head_sha": self.head,
                "base_ref": self.base,
                "head_repo": REPO,
                "is_cross_repository": False,
                "comments": [],
                "total_count": 0,
                "truncated": False,
            }

        async def publish_review(self, request):
            self.commands.append(request)
            if self.fail:
                raise TimeoutError("acknowledgement unknown")

    config = ModelPrReviewTriggerConfig(
        repositories=[REPO],
        base_branches=["dev"],
        scm_bus={"kind": "inmemory"},
        read_timeout_seconds=5,
        state_subdirectory="review-commands",
    )
    bus = Bus()
    env = {"ONEX_STATE_DIR": str(tmp_path), "OMNI_HOME": str(tmp_path)}
    event = ModelPrReviewObservation(repo=REPO, pr_number=PR)
    handler = HandlerPrReviewTrigger(config=config, bus=bus, environ=env)
    first = await handler.handle(event)
    assert first.outcome == "requested"
    assert (
        first.command_id
        == hashlib.sha256(f"{REPO.lower()}#{PR}#{HEAD}".encode()).hexdigest()
    )
    assert bus.commands[0].head_sha == HEAD
    # A new process instance sees the acknowledgement written by the first.
    repeated = await HandlerPrReviewTrigger(config=config, bus=bus, environ=env).handle(
        event
    )
    assert repeated.reason == "already_requested"
    assert len(bus.commands) == 1
    bus.head = OTHER_HEAD
    bus.fail = True
    with pytest.raises(TimeoutError, match="acknowledgement unknown"):
        await handler.handle(event)
    bus.fail = False
    retried = await handler.handle(event)
    assert retried.outcome == "requested"
    assert bus.commands[-1] == bus.commands[-2]
    assert retried.command_id != first.command_id
    old = await handler.handle(
        ModelPrReviewObservation(repo=REPO, pr_number=PR, head_sha=HEAD)
    )
    assert old.reason == "stale_observation"
    bus.state = "CLOSED"
    assert (await handler.handle(event)).reason == "not_open"
    bus.state = "OPEN"
    bus.base = "other"
    assert (await handler.handle(event)).reason == "base_out_of_scope"
    other = ModelPrReviewObservation(repo="example/other", pr_number=PR)
    calls = len(bus.calls)
    assert (await handler.handle(other)).reason == "repository_out_of_scope"
    assert len(bus.calls) == calls


def test_receipt_reader_waits_for_current_head_and_times_out_closed() -> None:
    from omnimarket.nodes.node_agent_review_gate_accept_effect.models.model_gate_accept import (
        ModelReceiptGateRequest,
    )

    clock = [0.0]
    reviews = FakeReviews([])

    def sleep(seconds):
        clock[0] += seconds
        reviews._reviews = [receipt_review("ready")]

    gate = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=reviews,
        verifier=FakeVerifier({"ready": accepted("ready")}),
        files=FakeFiles(),
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    request = ModelReceiptGateRequest(
        **receipt_request().model_dump(exclude={"wait_seconds", "poll_seconds"}),
        wait_seconds=12,
        poll_seconds=5,
    )
    result = gate.handle_receipt(request)
    assert result.decision is Decision.PASS_AGENT
    assert clock[0] == 5
    assert len(reviews.reads) == 2
    empty = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([]),
        verifier=FakeVerifier(),
        files=FakeFiles(),
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    result = empty.handle_receipt(request)
    assert result.decision is Decision.REFUSE
    assert result.reason.value == "receipt_timeout"
    assert clock[0] == 17


async def test_runtime_discovers_and_dispatches_pr_state_route(
    monkeypatch, tmp_path
) -> None:
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
    from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers.handler_pr_review_trigger import (
        HandlerPrReviewTrigger,
    )
    from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models.model_pr_review_trigger import (
        ModelPrReviewTriggerConfig,
    )

    commands = []

    class Gateway:
        async def call(self, node, request):
            return {
                "repo": REPO,
                "pr_number": PR,
                "title": "",
                "body": "",
                "body_truncated": False,
                "state": "OPEN",
                "is_draft": False,
                "merged": False,
                "author": "author",
                "head_ref": "feature",
                "head_sha": HEAD,
                "base_ref": "dev",
                "head_repo": REPO,
                "is_cross_repository": False,
                "comments": [],
                "total_count": 0,
                "truncated": False,
            }

        async def publish_review(self, request):
            commands.append(request)

    original = HandlerPrReviewTrigger.__init__

    def construct(self):
        original(
            self,
            config=ModelPrReviewTriggerConfig(
                repositories=[REPO],
                base_branches=["dev"],
                scm_bus={"kind": "inmemory"},
                read_timeout_seconds=5,
                state_subdirectory="commands",
            ),
            bus=Gateway(),
            environ={"OMNI_HOME": str(tmp_path), "ONEX_STATE_DIR": str(tmp_path)},
        )

    monkeypatch.setattr(HandlerPrReviewTrigger, "__init__", construct)
    manifest = discover_contracts_from_paths(
        [
            ROOT
            / "src/omnimarket/nodes/node_agent_review_dispatch_orchestrator/contract.yaml"
        ]
    )
    contract = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_agent_review_dispatch_orchestrator/contract.yaml"
        ).read_text()
    )
    topic = contract["handler_routing"]["handlers"][-1]["topic"]
    bus = EventBusInmemory(environment="review-proof", group="review-proof")
    await bus.start()
    try:
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest, engine, event_bus=bus, environment="local"
        )
        assert report.total_failed == 0
        assert report.total_wired >= 1
        engine.freeze()
        for _ in range(2):
            envelope = ModelEventEnvelope(
                payload={"repo": REPO, "pr_number": PR, "head_sha": HEAD},
                correlation_id=uuid4(),
                event_type=event_type_for(topic),
            )
            result = await engine.dispatch(topic=topic, envelope=envelope)
            assert result.status.value == "success"
        assert len(commands) == 1
        assert commands[0].head_sha == HEAD
        # Exercise the production result applier as the broker callback does;
        # a producer decision must never masquerade as a review completion.
        from omnibase_infra.runtime.service_dispatch_result_applier import (
            build_contract_result_applier,
        )

        published = []

        class Results:
            async def publish(self, topic, key, value, headers=None):
                published.append(topic)

            async def publish_envelope(self, envelope, topic, key=None):
                published.append(topic)

        applier = build_contract_result_applier(
            event_bus=Results(),
            contract_path=ROOT
            / "src/omnimarket/nodes/node_agent_review_dispatch_orchestrator/contract.yaml",
            publish_topics=contract["event_bus"]["publish_topics"],
            output_topic=contract["terminal_event"],
        )
        await applier.apply(result)
        assert len(published) == 1
        assert published[0].endswith("agent-review-pr-trigger-decided.v1")
    finally:
        await bus.close()


async def test_runtime_discovers_receipt_only_operation(monkeypatch) -> None:
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
    from omnimarket.nodes.node_agent_review_gate_accept_effect.handlers.handler_receipt_gate import (
        HandlerReceiptGate,
    )

    files = FakeFiles()
    handler = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=FakeReviews([receipt_review("ready")]),
        verifier=FakeVerifier({"ready": accepted("ready")}),
        files=files,
    )
    original = HandlerReceiptGate.__init__

    def construct(self):
        original(self, handler)

    monkeypatch.setattr(HandlerReceiptGate, "__init__", construct)
    path = (
        ROOT / "src/omnimarket/nodes/node_agent_review_gate_accept_effect/contract.yaml"
    )
    manifest = discover_contracts_from_paths([path])
    topic = yaml.safe_load(path.read_text())["handler_routing"]["handlers"][-1]["topic"]
    bus = EventBusInmemory(environment="receipt-proof", group="receipt-proof")
    await bus.start()
    try:
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest, engine, event_bus=bus, environment="local"
        )
        assert report.total_failed == 0
        assert report.total_wired >= 1
        engine.freeze()
        envelope = ModelEventEnvelope(
            payload=receipt_request().model_dump(mode="json"),
            correlation_id=uuid4(),
            event_type=event_type_for(topic),
        )
        result = await engine.dispatch(topic=topic, envelope=envelope)
        assert result.status.value == "success"
        assert "decision=pass_agent" in files.files[OUTPUT_PATH]
    finally:
        await bus.close()
