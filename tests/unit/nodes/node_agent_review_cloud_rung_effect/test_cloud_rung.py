# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cloud rung casts one unsigned vote from a review-only cloud session (OMN-22345).

Every case runs on both overlay profiles: the in-memory bus and, under the kafka marker,
the Redpanda bus. The delegation event is observed on the bus, never assumed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from pydantic import ValidationError

from omnimarket.models.agent_review_cloud import (
    REVIEW_HEAD_PREFIX,
    REVIEW_MARKER,
    REVIEW_ONLY_KIND,
    ModelCloudReview,
    ModelCloudReviewFinding,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_agent_review_cloud_rung_effect.adapters import (
    BusDelegationEventSink,
    RemoteLaneCloudCaller,
    lane_run_from_receipt,
)
from omnimarket.nodes.node_agent_review_cloud_rung_effect.handlers.handler_cloud_rung import (
    SIGNING_SECRET_NAME,
    CloudRungRefusedError,
    HandlerAgentReviewCloudRung,
    load_config,
    load_topics,
    render_review_brief,
    runner_environment,
    secret_leaks,
)
from omnimarket.nodes.node_agent_review_cloud_rung_effect.models import (
    EnumCloudRungTransport,
    ModelCloudEmitReceipt,
    ModelCloudLaneRun,
    ModelCloudRungConfig,
    ModelCloudRungTopics,
    ModelCloudVote,
    ModelCloudVoteRequest,
)
from omnimarket.nodes.node_agent_review_cloud_rung_effect.protocols import (
    ProtocolCloudRungBus,
)
from tests.node_overlay_support import install_node_overlay

HEAD = "c" * 40
OTHER = "d" * 40
SECRET_VALUE = "sv-7f3e9a1c-unique-signing-value"
HOST_ENV = {
    SIGNING_SECRET_NAME: SECRET_VALUE,
    "HOME": "/srv/example-home-4b1d",
    "ONEX_HOST_ENV_FILE": "/srv/example-env-file-9c2e",
    "LINEAR_API_KEY": "lin-example-key-55aa",
    "GH_TOKEN": "gh-example-token-77bb",
    "KAFKA_SASL_PASSWORD": "kafka-example-pass-11cc",
    "PATH": "/srv/example-bin-0d0d",
}
OVERLAY: dict[str, Any] = {
    "transport": "in_memory",
    "ticket": "OMN-1",
    "parent": "example-orchestrator",
    "lane_prefix": "cloud-review",
    "runner_model": "sonnet",
    "timeout_min": 45,
    "harness": "claude-cloud",
    "task_type": "code_review",
    "bus_lane": "dev",
}
REQUEST = ModelCloudVoteRequest(
    identity="sonnet-cloud",
    model="claude-sonnet-5-5",
    repo="example-org/example-repo",
    pr=7,
    head_sha=HEAD,
)
DISPATCH_VOTER_REVIEW_FIELDS = (
    "identity",
    "endpoint",
    "model",
    "delegation_event_id",
    "findings",
)
"""The dispatch node's ModelAgentVoterReview fields, spelled here: a node never imports another node's models."""


def config(
    transport: EnumCloudRungTransport = EnumCloudRungTransport.IN_MEMORY,
) -> ModelCloudRungConfig:
    return ModelCloudRungConfig.model_validate(
        {**OVERLAY, "transport": transport.value}
    )


def review(head: str = HEAD) -> ModelCloudReview:
    return ModelCloudReview(
        head_sha=head,
        catalog_version="0.1.0",
        findings=(
            ModelCloudReviewFinding(
                path="src/x.py", line=3, severity="major", claim="T02"
            ),
        ),
        source="final_message",
    )


def run(**over: Any) -> ModelCloudLaneRun:
    fields: dict[str, Any] = {
        "run_id": "rlane-cloud-review-7",
        "status": "done",
        "session_id": "session_01Review",
        "session_url": "https://claude.ai/code/session_01Review",
        "model": "claude-sonnet-5-5",
        "review": review(),
    }
    return ModelCloudLaneRun(**{**fields, **over})


class FakeLaneCaller:
    def __init__(self, result: ModelCloudLaneRun) -> None:
        self.result = result
        self.briefs: list[str] = []

    async def run(
        self,
        brief: str,
        lane: str,
        request: ModelCloudVoteRequest,
        cfg: ModelCloudRungConfig,
    ) -> ModelCloudLaneRun:
        self.briefs.append(brief)
        return self.result


class InMemoryBus:
    """The in-memory overlay's bus: a topic is a list of subscribers, publishing calls them in order."""

    def __init__(self) -> None:
        self.subscribers: dict[
            str, list[Callable[[ModelEventMessage], Awaitable[None]]]
        ] = {}

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        message = ModelEventMessage(
            topic=topic,
            key=key,
            value=value,
            headers=ModelEventHeaders(
                source="cloud-rung-test", event_type=topic, timestamp=datetime.now(UTC)
            ),
        )
        for callback in list(self.subscribers.get(topic, [])):
            await callback(message)
        return None

    async def subscribe_ungrouped(
        self, topic: str, on_message: Callable[[ModelEventMessage], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]:
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)

        return unsubscribe


class SilentSink:
    async def emit(
        self, topic: str, key: str, payload: dict[str, object]
    ) -> ModelCloudEmitReceipt:
        return ModelCloudEmitReceipt(
            event_id=key, published=False, detail="broker refused"
        )


class BrokenBus:
    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        raise ConnectionError("down")


def unique_topics() -> ModelCloudRungTopics:
    base = load_topics()
    suffix = uuid4().hex[:12]
    return ModelCloudRungTopics(
        command=f"{base.command.removesuffix('.v1')}.{suffix}.v1",
        delegation_completed=f"{base.delegation_completed.removesuffix('.v1')}.{suffix}.v1",
    )


async def cast_over(
    bus: ProtocolCloudRungBus,
    transport: EnumCloudRungTransport,
    lane_run: ModelCloudLaneRun,
) -> tuple[ModelCloudVote, dict[str, Any], FakeLaneCaller]:
    """Cast one vote with the bus-bound sink and return the vote, the delegation event observed on the bus and the runner."""
    topics = unique_topics()
    seen: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def observe(message: ModelEventMessage) -> None:
        await seen.put(json.loads(message.value))

    stop = await bus.subscribe_ungrouped(topics.delegation_completed, observe)
    runner = FakeLaneCaller(lane_run)
    try:
        handler = HandlerAgentReviewCloudRung(
            runner=runner,
            sink=BusDelegationEventSink(bus),
            config=config(transport),
            environ=HOST_ENV,
            topics=topics,
        )
        vote = await handler.handle(REQUEST)
        event = await asyncio.wait_for(seen.get(), timeout=30)
    finally:
        await stop()
    return vote, event, runner


async def assert_cloud_rung(
    bus: ProtocolCloudRungBus, transport: EnumCloudRungTransport
) -> None:
    # A good run: one unsigned vote whose event id is the delegation event on the bus.
    vote, event, runner = await cast_over(bus, transport, run())
    assert vote.identity == "sonnet-cloud"
    assert (vote.endpoint, vote.model) == ("claude.ai", "claude-sonnet-5-5")
    assert vote.delegation_event_id == event["correlation_id"]
    assert [(f.path, f.line, f.severity) for f in vote.findings] == [
        ("src/x.py", 3, "major")
    ]
    assert tuple(type(vote).model_fields) == DISPATCH_VOTER_REVIEW_FIELDS, (
        "the dispatch node's voter review shape"
    )
    assert "signature" not in vote.model_dump()
    assert event["backend_id"] == "harness:claude-cloud:claude-sonnet-5-5"
    response = json.loads(event["response"])
    assert (response["run_id"], response["session_id"], response["head_sha"]) == (
        "rlane-cloud-review-7",
        "session_01Review",
        HEAD,
    )
    ModelDelegateSkillTerminalProjection.model_validate(event)
    brief = runner.briefs[0]
    assert f"Lane kind: {REVIEW_ONLY_KIND}" in brief
    assert f"{REVIEW_HEAD_PREFIX} {HEAD}" in brief
    assert f"{REVIEW_MARKER} head={HEAD}" in brief

    # A model or endpoint other than the voter's is returned as written, never rewritten.
    other, _event, _runner = await cast_over(
        bus,
        transport,
        run(
            model="claude-opus-5-5",
            session_url="https://other.example.test/code/session_01Review",
        ),
    )
    assert (other.model, other.endpoint) == ("claude-opus-5-5", "other.example.test")

    # A review of another head is refused, and no event is emitted for it.
    with pytest.raises(CloudRungRefusedError, match="head"):
        await cast_over(bus, transport, run(review=review(OTHER)))

    # A lane that returned no review casts no vote.
    with pytest.raises(CloudRungRefusedError, match="cloud-review-unparsed"):
        await cast_over(
            bus,
            transport,
            run(status="cloud-review-unparsed", review=None, refusal="no_marker"),
        )


async def test_cloud_rung_on_the_in_memory_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "node_agent_review_cloud_rung_effect", OVERLAY
    )
    assert load_config().transport is EnumCloudRungTransport.IN_MEMORY, (
        "the overlay selects the bus binding"
    )
    await assert_cloud_rung(InMemoryBus(), EnumCloudRungTransport.IN_MEMORY)


@pytest.mark.kafka
@pytest.mark.integration
async def test_cloud_rung_on_the_redpanda_overlay(
    kafka_integration_bus: ProtocolCloudRungBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_agent_review_cloud_rung_effect",
        {**OVERLAY, "transport": "redpanda"},
    )
    assert load_config().transport is EnumCloudRungTransport.REDPANDA
    await assert_cloud_rung(kafka_integration_bus, EnumCloudRungTransport.REDPANDA)


# -- no delegation event, no vote ---------------------------------------------------------


@pytest.mark.parametrize("sink", [SilentSink(), BusDelegationEventSink(BrokenBus())])
async def test_no_delegation_event_means_no_vote(sink: Any) -> None:
    handler = HandlerAgentReviewCloudRung(
        runner=FakeLaneCaller(run()), sink=sink, config=config(), environ=HOST_ENV
    )
    with pytest.raises(CloudRungRefusedError, match="no delegation event"):
        await handler.handle(REQUEST)


# -- the signing secret never reaches the session -------------------------------------------


def test_the_brief_holds_neither_the_secret_name_nor_any_host_env_value() -> None:
    brief = render_review_brief(REQUEST, "cloud-review-7-cccccccc-abc123", config())
    assert SIGNING_SECRET_NAME not in brief
    for name, value in HOST_ENV.items():
        assert value not in brief, name
    assert secret_leaks(brief, HOST_ENV) == []
    assert secret_leaks(brief + SECRET_VALUE, HOST_ENV) == [SIGNING_SECRET_NAME]
    assert secret_leaks(brief + "GH_TOKEN", HOST_ENV) == ["GH_TOKEN"]


async def test_a_brief_holding_secret_material_is_refused_before_any_session_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_agent_review_cloud_rung_effect.handlers import (
        handler_cloud_rung,
    )

    monkeypatch.setattr(
        handler_cloud_rung,
        "render_review_brief",
        lambda *_args: f"brief {SECRET_VALUE}",
    )
    runner = FakeLaneCaller(run())
    handler = HandlerAgentReviewCloudRung(
        runner=runner, sink=SilentSink(), config=config(), environ=HOST_ENV
    )
    with pytest.raises(CloudRungRefusedError, match=SIGNING_SECRET_NAME):
        await handler.handle(REQUEST)
    assert runner.briefs == [], "no session was started"


def test_the_runner_environment_drops_every_secret_name() -> None:
    child = runner_environment(HOST_ENV)
    assert set(child) == {"HOME", "ONEX_HOST_ENV_FILE", "PATH"}
    assert SECRET_VALUE not in json.dumps(child)


def test_a_vote_never_carries_a_signature() -> None:
    with pytest.raises(ValidationError):
        ModelCloudVote.model_validate(
            {
                "identity": "i",
                "endpoint": "e",
                "model": "m",
                "delegation_event_id": "x",
                "signature": "c2ln",
            }
        )


# -- the production runner binding, run as a real child process ------------------------------

STUB_RUNNER = textwrap.dedent(
    """
    import json, os, sys
    args = sys.argv[1:]
    receipt = args[args.index("--receipt") + 1]
    brief = open(args[args.index("--brief") + 1], encoding="utf-8").read()
    head = [l.split()[-1] for l in brief.splitlines() if l.startswith("Review head:")][0]
    json.dump({"run_id": "rlane-stub", "status": "done", "argv": args, "environ": dict(os.environ),
               "cloud": {"session_id": "session_01Stub", "url": "https://claude.ai/code/session_01Stub",
                         "session_state": {"model": "claude-sonnet-5-5"},
                         "review": {"head_sha": head, "catalog_version": "0.1.0", "findings": [], "source": "final_message",
                                    "model": "claude-sonnet-5-5"}}}, open(receipt, "w"))
    """
)


async def test_the_remote_lane_binding_starts_the_runner_with_no_secret_and_reads_its_receipt(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    script = plugin / "skills" / "remote-lane" / "scripts" / "onex_remote_lane.py"
    script.parent.mkdir(parents=True)
    script.write_text(STUB_RUNNER, encoding="utf-8")
    environ = {
        **HOST_ENV,
        "PATH": "/usr/bin:/bin",
        "OMNI_PLUGIN_ROOT": str(plugin),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    runner = RemoteLaneCloudCaller(environ)
    brief = render_review_brief(REQUEST, "cloud-review-stub", config())
    lane_run = await runner.run(brief, "cloud-review-stub", REQUEST, config())
    assert lane_run.status == "done"
    assert lane_run.review is not None
    assert lane_run.review.head_sha == HEAD
    assert (lane_run.session_url, lane_run.model) == (
        "https://claude.ai/code/session_01Stub",
        "claude-sonnet-5-5",
    )
    receipt = json.loads(next((tmp_path / "state").rglob("receipt.json")).read_text())
    assert SIGNING_SECRET_NAME not in receipt["environ"]
    assert SECRET_VALUE not in json.dumps(receipt["environ"])
    assert {"GH_TOKEN", "LINEAR_API_KEY", "KAFKA_SASL_PASSWORD"}.isdisjoint(
        receipt["environ"]
    )
    argv = receipt["argv"]
    assert argv[:1] == ["run"]
    assert argv[argv.index("--placement") + 1] == "cloud"
    assert (argv[argv.index("--repo") + 1], argv[argv.index("--owner") + 1]) == (
        "example-repo",
        "example-org",
    )
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert sys.executable  # the runner ran under this interpreter


def test_a_receipt_with_a_refused_review_names_the_refusal() -> None:
    lane_run = lane_run_from_receipt(
        {
            "run_id": "r",
            "status": "cloud-review-unparsed",
            "cloud": {"review": {"refused": "no_marker", "detail": "no line"}},
        }
    )
    assert lane_run.review is None
    assert lane_run.refusal == "no_marker: no line"
    assert (
        lane_run_from_receipt({"status": "failed", "error": "no account"}).refusal
        == "no account"
    )


async def test_the_handler_builds_with_no_arguments_and_binds_its_production_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runtime constructs a handler with no arguments; every port then binds from the overlay and environment."""
    from omnimarket.nodes.node_agent_review_cloud_rung_effect.adapters import (
        CloudRungNotBoundError,
        LaneBusDelegationEventSink,
    )

    install_node_overlay(
        monkeypatch, tmp_path, "node_agent_review_cloud_rung_effect", OVERLAY
    )
    monkeypatch.delenv("OMNI_PLUGIN_ROOT", raising=False)
    handler = HandlerAgentReviewCloudRung()
    runner, sink, cfg, _environ, topics = handler._bound()
    assert isinstance(runner, RemoteLaneCloudCaller)
    assert isinstance(sink, LaneBusDelegationEventSink)
    assert cfg.ticket == "OMN-1"
    assert topics == load_topics()
    with pytest.raises(CloudRungNotBoundError, match="OMNI_PLUGIN_ROOT"):
        await handler.handle(REQUEST)


def test_the_contract_declares_the_rung_topics() -> None:
    """The handler spells no topic: the command, the vote and the delegation terminal are the contract's."""
    from omnimarket.nodes.node_agent_review_cloud_rung_effect.handlers.handler_cloud_rung import (
        load_contract,
    )

    assert load_topics() == ModelCloudRungTopics(
        command="onex.cmd.omnimarket.agent-review-cloud-vote-requested.v1",
        delegation_completed="onex.evt.omnimarket.delegate-skill-completed.v1",
    )
    contract = load_contract()
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.agent-review-cloud-vote-cast.v1",
        "onex.evt.omnimarket.delegate-skill-completed.v1",
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.agent-review-cloud-vote-cast.v1"
    )
