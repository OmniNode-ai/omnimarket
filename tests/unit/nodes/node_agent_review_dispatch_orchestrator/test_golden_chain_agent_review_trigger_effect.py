# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded no-voter case: a required head needs a command, never a green check."""

from __future__ import annotations

# Ports below preserve the measured log's shape while using fixture identities.
# The live log's endpoints and voter names are deployment data, not node policy.
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.ci_red_triage import (
    ModelCiRunFailedEvent,
    ci_run_failed_event_id,
)
from omnimarket.models.model_agent_review_command import ModelAgentReviewRequest
from omnimarket.nodes.node_agent_review_trigger_effect.handlers.handler_dispatch_journal import (
    FileDispatchJournal,
)
from omnimarket.nodes.node_agent_review_trigger_effect.handlers.handler_review_trigger import (
    HandlerAgentReviewTrigger,
    command_for,
    no_voter_verdict,
)
from omnimarket.nodes.node_agent_review_trigger_effect.handlers.handler_review_trigger_bus import (
    ReviewTriggerBus,
    ScmReviewTriggerSource,
)
from omnimarket.nodes.node_agent_review_trigger_effect.models.model_review_trigger import (
    ModelReviewTriggerConfig,
    ModelReviewTriggerEvidence,
)
from tests.unit.nodes.node_agent_review_dispatch_orchestrator.support import (
    HEAD,
    MOVED,
    PR,
    REPO,
    InMemoryBus,
    build_harness,
)

ROOT = Path(__file__).resolve().parents[4]
TOPIC = "onex.cmd.omnimarket.agent-review-requested.v1"
RECORDED_LOG = """Quorum verdict: no_models (threshold=2, models succeeded=0, distinct reviewers=0)
Hostile review gate accept: decision=unchanged reason=no_receipt_review; unsuccessful voters: first=unavailable, second=unavailable
"""


def test_recorded_no_voters_has_a_command_producer() -> None:
    producers = []
    for path in (ROOT / "src/omnimarket/nodes").glob("*/contract.yaml"):
        text = path.read_text()
        if TOPIC not in text:
            continue
        contract = yaml.safe_load(text)
        if TOPIC in (contract.get("event_bus") or {}).get("publish_topics", []):
            producers.append(contract["name"])
    assert "node_agent_review_trigger_effect" in producers, (
        "recorded quorum_verdict=no_models and decision=unchanged "
        "reason=no_receipt_review have no command producer"
    )


CHECK = "fixture-required-review"
JOB = "fixture-voter-job"


def config() -> ModelReviewTriggerConfig:
    return ModelReviewTriggerConfig(
        repository_owner=REPO.partition("/")[0],
        required_review_checks=(CHECK,),
        reviewer_jobs=(JOB,),
        reviewer_workflow="fixture-review.yml",
        bus_lane="fixture-lane",
        timeout_seconds=1,
        lock_poll_seconds=0.001,
        read_limit=50,
        log_tail_bytes=65536,
        state_subdirectory="fixture-trigger",
    )


def event(
    *, repo: str = REPO, pr: int = PR, head: str = HEAD, run: int = 81
) -> ModelCiRunFailedEvent:
    return ModelCiRunFailedEvent(
        event_id=ci_run_failed_event_id(repo, pr, head, (CHECK,)),
        repo=repo,
        pr_number=pr,
        head_sha=head,
        base="fixture-base",
        armed=True,
        queued=False,
        failing_checks=(CHECK,),
        ci_read_at="2026-11-05T05:32:08Z",
        observed_at="2026-11-05T05:32:08Z",
        source_digest="fixture",
        failing_runs=(
            {
                "check": CHECK,
                "run_id": run,
                "workflow": "fixture-review",
                "conclusion": "failure",
                "completed_at": "2026-11-05T05:32:08Z",
            },
        ),
    )


class Source:
    def __init__(
        self, *, log: str = RECORDED_LOG, required: bool = True, head: str = HEAD
    ) -> None:
        self.log = log
        self.required = required
        self.head = head
        self.reads = 0

    async def evidence(
        self, observation: ModelCiRunFailedEvent
    ) -> tuple[ModelReviewTriggerEvidence, ...]:
        self.reads += 1
        return (
            ModelReviewTriggerEvidence(
                head_sha=HEAD, required=self.required, log_text=self.log
            ),
        )

    async def read_head(self, repo: str, pr: int) -> str:
        self.reads += 1
        return self.head


class Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.commands: list[ModelAgentReviewRequest] = []
        self.fail = fail

    async def publish(self, request: ModelAgentReviewRequest) -> None:
        self.commands.append(request)
        if self.fail:
            raise ConnectionError("fixture ambiguous acknowledgement")


def journal(root: Path) -> FileDispatchJournal:
    return FileDispatchJournal(root, poll_seconds=0.001)


async def test_recorded_case_one_command_one_receipt_repeat_does_nothing(
    profile: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    h = build_harness(profile, monkeypatch, tmp_path)
    bus = InMemoryBus()
    published = []

    async def dispatch(message: Any) -> None:
        wire = json.loads(message.value)
        command = ModelAgentReviewRequest.model_validate(wire["payload"])
        published.append(command)
        terminal = await h.run(command)
        assert terminal.outcome == "completed"

    await bus.subscribe_ungrouped(TOPIC, dispatch)

    @asynccontextmanager
    async def opened() -> Any:
        yield bus

    publisher = ReviewTriggerBus(config(), {})
    monkeypatch.setattr(publisher, "_open", opened)
    source = Source()
    handler = HandlerAgentReviewTrigger(
        config=config(),
        source=source,
        publisher=publisher,
        journal=journal(tmp_path / "journal"),
    )
    assert h.world.stored == {}
    assert no_voter_verdict(RECORDED_LOG)
    first = await handler.handle(event())
    assert first.outcome == "emitted"
    assert len(published) == 1
    assert len(h.world.stored) == 1
    stored = next(iter(h.world.stored.values()))
    assert h.world.posts == [stored.id]
    calls = h.world.total_calls()
    reads = source.reads
    # A fresh handler and journal instance stand for the next runtime process.
    again = HandlerAgentReviewTrigger(
        config=config(),
        source=source,
        publisher=publisher,
        journal=journal(tmp_path / "journal"),
    )
    result = await again.handle(event(run=82))
    assert result.reason == "already_requested"
    assert len(published) == 1
    assert source.reads == reads
    assert h.world.total_calls() == calls
    assert len(h.world.stored) == 1
    assert h.world.posts == [stored.id]


@pytest.mark.parametrize(
    "change",
    ["one_answer", "passed", "blocked", "receipt", "no_receipt_line", "no_quorum_line"],
)
async def test_other_verdicts_do_not_emit(change: str, tmp_path: Path) -> None:
    logs = {
        "one_answer": RECORDED_LOG.replace("no_models", "degraded_quorum").replace(
            "models succeeded=0", "models succeeded=1"
        ),
        "passed": RECORDED_LOG.replace("no_models", "passed"),
        "blocked": RECORDED_LOG.replace("no_models", "blocked"),
        "receipt": RECORDED_LOG.replace(
            "decision=unchanged reason=no_receipt_review",
            "decision=pass_agent reason=receipt_accepted",
        ),
        "no_receipt_line": RECORDED_LOG.splitlines()[0],
        "no_quorum_line": RECORDED_LOG.splitlines()[-1],
    }
    publisher = Publisher()
    handler = HandlerAgentReviewTrigger(
        config=config(),
        source=Source(log=logs[change]),
        publisher=publisher,
        journal=journal(tmp_path),
    )
    result = await handler.handle(event())
    assert result.reason == "no_eligible_evidence"
    assert publisher.commands == []


@pytest.mark.parametrize(
    ("required", "head", "reason"),
    [(False, HEAD, "no_eligible_evidence"), (True, MOVED, "stale_head")],
)
async def test_required_and_current_head_are_observed(
    required: bool, head: str, reason: str, tmp_path: Path
) -> None:
    publisher = Publisher()
    handler = HandlerAgentReviewTrigger(
        config=config(),
        source=Source(required=required, head=head),
        publisher=publisher,
        journal=journal(tmp_path),
    )
    result = await handler.handle(event())
    assert result.reason == reason
    assert publisher.commands == []
    assert not journal(tmp_path).sent(result.command_id)


async def test_concurrent_delivery_and_independent_identities(tmp_path: Path) -> None:
    publisher = Publisher()
    handlers = [
        HandlerAgentReviewTrigger(
            config=config(),
            source=Source(),
            publisher=publisher,
            journal=journal(tmp_path),
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(*(h.handle(event()) for h in handlers))
    assert sorted(r.outcome for r in results) == ["emitted", "unchanged"]
    assert len(publisher.commands) == 1
    same = command_for(event(), config())
    assert same == command_for(event(repo=REPO.lower(), run=101), config())
    assert (
        len(
            {
                command_for(e, config()).command_id
                for e in [
                    event(),
                    event(repo="other/repo"),
                    event(pr=PR + 1),
                    event(head=MOVED),
                ]
            }
        )
        == 4
    )


async def test_publish_failure_retries_same_logical_command(tmp_path: Path) -> None:
    publisher = Publisher(fail=True)
    handler = HandlerAgentReviewTrigger(
        config=config(), source=Source(), publisher=publisher, journal=journal(tmp_path)
    )
    with pytest.raises(ConnectionError, match="ambiguous"):
        await handler.handle(event())
    assert not journal(tmp_path).sent(command_for(event(), config()).command_id)
    publisher.fail = False
    await handler.handle(event(run=82))
    assert len(publisher.commands) == 2
    assert publisher.commands[0] == publisher.commands[1]
    assert (await handler.handle(event())).reason == "already_requested"


class Gateway:
    """Typed SCM terminal results; the source adapter itself runs unchanged."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.replies: dict[str, Any] = {
            "ci_checks": {
                "operation": "ci_checks",
                "repo": REPO,
                "pr_number": PR,
                "overall": "red",
                "total": 1,
                "passed": 0,
                "failed": 1,
                "pending": 0,
                "failing_contexts": [CHECK],
            },
            "workflow_runs": {
                "operation": "workflow_runs",
                "repo": REPO,
                "truncated": False,
                "runs": [
                    {
                        "id": 81,
                        "name": "fixture-review",
                        "workflow_id": 9,
                        "head_sha": HEAD,
                        "event": "pull_request",
                        "status": "completed",
                        "conclusion": "failure",
                        "run_number": 2,
                        "run_attempt": 1,
                        "created_at": "2026-11-05T05:30:00Z",
                        "updated_at": "2026-11-05T05:32:00Z",
                        "html_url": "https://example.invalid/run",
                    }
                ],
            },
            "run_jobs": {
                "operation": "run_jobs",
                "repo": REPO,
                "run_id": 81,
                "truncated": False,
                "jobs": [
                    {
                        "id": 91,
                        "name": JOB,
                        "status": "completed",
                        "conclusion": "failure",
                        "run_attempt": 1,
                    }
                ],
            },
            "job_log_tail": {
                "operation": "job_log_tail",
                "repo": REPO,
                "job_id": 91,
                "total_bytes": len(RECORDED_LOG),
                "truncated": False,
                "text": RECORDED_LOG,
            },
            "merged_pr": {
                "operation": "merged_pr",
                "repo": REPO,
                "pr_number": PR,
                "state": "OPEN",
                "merged": False,
                "head_sha": HEAD,
                "head_ref": "fixture",
                "base_ref": "fixture-base",
            },
        }

    async def read(self, request: Any, model: Any) -> Any:
        self.reads.append(request.operation.value)
        return model.model_validate(self.replies[request.operation.value])


async def test_production_source_reads_required_exact_head_and_same_attempt() -> None:
    gateway = Gateway()
    source = ScmReviewTriggerSource(gateway, config())
    evidence = await source.evidence(event())
    assert len(evidence) == 1
    assert evidence[0].required
    assert no_voter_verdict(evidence[0].log_text)
    assert await source.read_head(REPO, PR) == HEAD
    assert gateway.reads == [
        "ci_checks",
        "workflow_runs",
        "run_jobs",
        "job_log_tail",
        "merged_pr",
    ]


@pytest.mark.parametrize(
    "poison",
    [
        "not_required",
        "wrong_head",
        "wrong_attempt",
        "truncated_runs",
        "truncated_jobs",
        "newer_run",
        "merge_group",
    ],
)
async def test_production_source_refuses_stale_or_unread_evidence(poison: str) -> None:
    gateway = Gateway()
    if poison == "not_required":
        gateway.replies["ci_checks"]["failing_contexts"] = []
    if poison == "wrong_head":
        gateway.replies["workflow_runs"]["runs"][0]["head_sha"] = MOVED
    if poison == "wrong_attempt":
        gateway.replies["run_jobs"]["jobs"][0]["run_attempt"] = 2
    if poison == "truncated_runs":
        gateway.replies["workflow_runs"]["truncated"] = True
    if poison == "truncated_jobs":
        gateway.replies["run_jobs"]["truncated"] = True
    if poison == "merge_group":
        gateway.replies["workflow_runs"]["runs"][0]["event"] = "merge_group"
    if poison == "newer_run":
        newer = copy.deepcopy(gateway.replies["workflow_runs"]["runs"][0])
        newer.update(id=82, run_number=3)
        gateway.replies["workflow_runs"]["runs"].append(newer)
    source = ScmReviewTriggerSource(gateway, config())
    assert await source.evidence(event()) == ()
    assert "job_log_tail" not in gateway.reads


async def test_completed_review_job_need_not_wait_for_the_workflow_to_finish() -> None:
    gateway = Gateway()
    gateway.replies["workflow_runs"]["runs"][0].update(
        status="in_progress", conclusion=None
    )
    source = ScmReviewTriggerSource(gateway, config())
    assert len(await source.evidence(event())) == 1


async def test_watcher_observation_without_run_details_still_reads_exact_head() -> None:
    gateway = Gateway()
    source = ScmReviewTriggerSource(gateway, config())
    assert (
        len(await source.evidence(event().model_copy(update={"failing_runs": ()}))) == 1
    )


async def test_measured_watcher_short_repo_and_no_run_facts_use_overlay_owner() -> None:
    observation = event(repo=REPO.partition("/")[-1]).model_copy(
        update={"failing_runs": ()}
    )
    gateway = Gateway()
    source = ScmReviewTriggerSource(gateway, config())
    assert len(await source.evidence(observation)) == 1
    assert command_for(observation, config()) == command_for(event(), config())


def test_trigger_node_has_no_deployment_literals() -> None:
    from .test_no_literals import literal_findings

    paths = (ROOT / "src/omnimarket/nodes/node_agent_review_trigger_effect").rglob(
        "*.py"
    )
    assert [
        finding
        for path in paths
        for finding in literal_findings(path.read_text(), path.name)
    ] == []


async def test_ambiguous_publish_replays_the_terminal_without_another_receipt(
    profile: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    h = build_harness(profile, monkeypatch, tmp_path)

    class AmbiguousPublisher(Publisher):
        async def publish(self, request: ModelAgentReviewRequest) -> None:
            self.commands.append(request)
            await h.run(request)
            if self.fail:
                raise ConnectionError("fixture acknowledgement lost after review")

    publisher = AmbiguousPublisher(fail=True)
    handler = HandlerAgentReviewTrigger(
        config=config(),
        source=Source(),
        publisher=publisher,
        journal=journal(tmp_path / "journal"),
    )
    with pytest.raises(ConnectionError, match="acknowledgement lost"):
        await handler.handle(event())
    assert len(h.world.stored) == 1
    assert len(h.world.posts) == 1
    calls = h.world.total_calls()
    publisher.fail = False
    assert (await handler.handle(event(run=82))).outcome == "emitted"
    assert len(publisher.commands) == 2
    assert len({c.command_id for c in publisher.commands}) == 1
    assert len(h.world.stored) == len(h.world.posts) == 1
    assert h.world.total_calls() == calls


async def test_required_overlay_binds_when_runtime_constructs_without_config(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "overlays" / "node_agent_review_trigger_effect"
    overlay.mkdir(parents=True)
    (overlay / "overlay.yaml").write_text(
        yaml.safe_dump(config().model_dump(mode="json"))
    )
    publisher = Publisher()
    handler = HandlerAgentReviewTrigger(
        source=Source(),
        publisher=publisher,
        environ={
            "ONEX_SKILL_OVERLAY_ROOTS": str(overlay.parent),
            "ONEX_STATE_DIR": str(tmp_path / "state"),
        },
    )
    assert (await handler.handle(event())).outcome == "emitted"
    assert (await handler.handle(event())).reason == "already_requested"
    assert len(publisher.commands) == 1


def test_runtime_discovers_the_trigger_and_shares_the_existing_command_model() -> None:
    from importlib.metadata import entry_points

    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )

    from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
        ModelAgentReviewRequest as DispatchRequest,
    )

    path = ROOT / "src/omnimarket/nodes/node_agent_review_trigger_effect/contract.yaml"
    manifest = discover_contracts_from_paths([path])
    assert not manifest.errors
    assert len(manifest.contracts) == 1
    assert manifest.contracts[0].name == "node_agent_review_trigger_effect"
    assert manifest.contracts[0].event_bus.subscribe_topics == (
        "onex.evt.omnimarket.ci-run-failed.v1",
    )
    assert TOPIC in manifest.contracts[0].event_bus.publish_topics
    assert DispatchRequest is ModelAgentReviewRequest
    points = [
        p
        for p in entry_points(group="onex.nodes")
        if p.name == "node_agent_review_trigger_effect"
    ]
    assert len(points) == 1
    assert points[0].load().NodeAgentReviewTriggerEffect is not None


def test_shipped_overlay_matches_the_typed_config() -> None:
    path = ROOT / "config/onex/overlays/node_agent_review_trigger_effect/overlay.yaml"
    shipped = ModelReviewTriggerConfig.model_validate(yaml.safe_load(path.read_text()))
    assert shipped.required_review_checks
    assert shipped.reviewer_jobs


async def test_previously_stored_manual_receipt_keeps_its_key_and_is_not_reviewed_again(
    profile: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    h = build_harness(profile, monkeypatch, tmp_path)
    manual = await h.run(h.request("fixture-manual-command"))
    lab_calls = list(h.world.lab_calls)
    assert len(h.world.stored) == 1

    class DispatchPublisher(Publisher):
        async def publish(self, request: ModelAgentReviewRequest) -> None:
            self.commands.append(request)
            terminal = await h.run(request)
            assert terminal.receipt_id == manual.receipt_id

    publisher = DispatchPublisher()
    handler = HandlerAgentReviewTrigger(
        config=config(),
        source=Source(),
        publisher=publisher,
        journal=journal(tmp_path / "journal"),
    )
    assert (await handler.handle(event(repo=REPO.lower()))).outcome == "emitted"
    assert publisher.commands[0].repo == REPO
    assert h.world.lab_calls == lab_calls
    assert len(h.world.stored) == 1
    assert h.world.posts == [manual.receipt_id]


async def test_canonical_runtime_wiring_dispatches_the_recorded_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    publisher = Publisher()
    original = HandlerAgentReviewTrigger.__init__

    def construct(self: HandlerAgentReviewTrigger) -> None:
        original(
            self,
            config=config(),
            source=Source(),
            publisher=publisher,
            journal=journal(tmp_path),
        )

    monkeypatch.setattr(HandlerAgentReviewTrigger, "__init__", construct)
    path = ROOT / "src/omnimarket/nodes/node_agent_review_trigger_effect/contract.yaml"
    manifest = discover_contracts_from_paths([path])
    topic = manifest.contracts[0].event_bus.subscribe_topics[0]
    bus = EventBusInmemory(environment="trigger-probe", group="trigger-probe")
    await bus.start()
    try:
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest, engine, event_bus=bus, environment="local"
        )
        assert report.total_wired == 1
        assert report.total_failed == 0
        engine.freeze()
        for _ in range(2):
            envelope = ModelEventEnvelope(
                payload=event().model_dump(mode="json"),
                correlation_id=uuid4(),
                event_type=event_type_for(topic),
            )
            result = await engine.dispatch(topic=topic, envelope=envelope)
            assert result.status.value == "success"
        assert len(publisher.commands) == 1
    finally:
        await bus.close()
