# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude review batch submits replay items through admission and emits a cost and a verdict per item (OMN-20910)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.models.review_replay.model_replay_manifest import (
    ModelReplayItem,
    ModelReplayManifest,
)
from omnimarket.nodes.node_claude_review_batch_effect.handlers.adapter_lane_bus import (
    BusEventSink,
)
from omnimarket.nodes.node_claude_review_batch_effect.handlers.handler_claude_review_batch import (
    HandlerClaudeReviewBatch,
)
from omnimarket.nodes.node_claude_review_batch_effect.handlers.stub_review_admission import (
    StubReviewAdmission,
)
from omnimarket.nodes.node_claude_review_batch_effect.models import (
    EnumBatchVerdict,
    ModelBatchCall,
    ModelBatchItemResult,
    ModelBatchPoll,
    ModelBatchUsage,
    ModelClaudeReviewBatchRequest,
    ModelReviewAdmissionDecision,
    ModelReviewAdmissionRequest,
)

pytestmark = pytest.mark.unit

CONTRACT = yaml.safe_load(
    (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_claude_review_batch_effect/contract.yaml"
    ).read_text(encoding="utf-8")
)
TOPICS = CONTRACT["event_bus"]["publish_topics"]

BLOCKING = json.dumps(
    [
        {
            "category": "correctness",
            "severity": "critical",
            "title": "off by one",
            "description": "the loop skips the last item",
            "evidence": "for i in range(n - 1)",
            "location": "a.py",
        }
    ]
)
CLEAN = "[]"


def _store(tmp_path: Path, repos: tuple[str, ...] = ("alpha", "beta")) -> Path:
    directory = tmp_path / "set"
    (directory / "diffs").mkdir(parents=True)
    items = []
    for number, repo in enumerate(repos, start=1):
        body = f"diff --git a/{repo}.py b/{repo}.py\n+change {number}\n"
        item_id = f"{repo}-{number}-{number:012x}"
        (directory / "diffs" / f"{item_id}.diff").write_text(body, encoding="utf-8")
        items.append(
            ModelReplayItem(
                item_id=item_id,
                repo=repo,
                pr_number=number,
                merge_sha=f"{number:040x}",
                parent_sha=f"{number + 100:040x}",
                title=f"change {number} (#{number})",
                changed_lines=2,
                size_bin="small",
                diff_sha256=hashlib.sha256(body.encode()).hexdigest(),
                diff_file=f"diffs/{item_id}.diff",
            )
        )
    manifest = ModelReplayManifest(
        seed="s", target_count=40, size_bin_upper_lines=(199, 1000), items=tuple(items)
    )
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")
    return path


class _Sink:
    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published.append((topic, event))


class _Transport:
    """Answers each submitted call with a canned review; ``ends_after`` polls before it ends."""

    def __init__(
        self,
        answers: dict[str, str] | None = None,
        *,
        mode: str = "batch",
        ends_after: int = 1,
        stop_reason: str = "end_turn",
        fail: set[str] | None = None,
    ) -> None:
        self.execution_mode = mode
        self.answers = answers or {}
        self.default = CLEAN
        self.ends_after = ends_after
        self.stop_reason = stop_reason
        self.fail = fail or set()
        self.submitted: list[ModelBatchCall] = []
        self.polls = 0

    def submit(self, calls: Sequence[ModelBatchCall]) -> str:
        self.submitted.extend(calls)
        return "batch-1"

    def poll(self, batch_id: str) -> ModelBatchPoll:
        self.polls += 1
        if self.polls < self.ends_after:
            return ModelBatchPoll(ended=False, results=())
        results = []
        for call in self.submitted:
            failed = call.item_id in self.fail
            results.append(
                ModelBatchItemResult(
                    item_id=call.item_id,
                    success=not failed,
                    content=None
                    if failed
                    else self.answers.get(call.item_id, self.default),
                    stop_reason=None if failed else self.stop_reason,
                    failure="upstream_error" if failed else None,
                    usage=ModelBatchUsage(
                        input_tokens=100,
                        cache_write_tokens=0,
                        cache_read_tokens=0,
                        output_tokens=20,
                    ),
                )
            )
        return ModelBatchPoll(ended=True, results=tuple(results))


class _Admission:
    def __init__(self, refuse: set[str] | None = None) -> None:
        self.refuse = refuse or set()
        self.requests: list[ModelReviewAdmissionRequest] = []
        self.settled: list[str] = []
        self.released: list[str] = []

    def admit(
        self, request: ModelReviewAdmissionRequest
    ) -> ModelReviewAdmissionDecision:
        self.requests.append(request)
        refused = request.item_id in self.refuse
        return ModelReviewAdmissionDecision(
            item_id=request.item_id,
            admitted=not refused,
            reason="over the aggregate cap" if refused else "within allowance",
            reservation_id=None if refused else f"res-{request.item_id}",
        )

    def settle(self, item_id: str, usage: ModelBatchUsage) -> None:
        self.settled.append(item_id)

    def release(self, item_id: str) -> None:
        self.released.append(item_id)


def _request(manifest: Path, **overrides: object) -> ModelClaudeReviewBatchRequest:
    fields: dict[str, Any] = {
        "correlation_id": "corr-1",
        "manifest_path": str(manifest),
        "model_id": "claude-test-model",
        "endpoint_ref": "https://voter.invalid/v1",
        "secret_ref": "llm.test.key",
        "provider": "anthropic",
        "model_tier": "cloud",
        "system_prompt": "review the diff",
        "max_tokens": 1456,
        "cloud_refused_repositories": (),
        "max_polls": 3,
        "poll_interval_seconds": 0.0,
    }
    fields.update(overrides)
    return ModelClaudeReviewBatchRequest(**fields)


def _handler(
    transport: _Transport, admission: _Admission, sink: _Sink
) -> HandlerClaudeReviewBatch:
    return HandlerClaudeReviewBatch(
        transport=transport, admission=admission, sink=sink, sleep=lambda _s: None
    )


def test_each_item_goes_admission_then_call_then_parse_then_verdict(
    tmp_path: Path,
) -> None:
    manifest = _store(tmp_path)
    first_id = "alpha-1-000000000001"
    transport = _Transport({first_id: BLOCKING})
    admission, sink = _Admission(), _Sink()

    result = _handler(transport, admission, sink).handle(_request(manifest))

    assert result.status == "completed"
    verdicts = {v.item_id: v for v in result.verdicts}
    assert verdicts[first_id].verdict is EnumBatchVerdict.BLOCKED
    assert verdicts[first_id].blocking_findings == 1
    assert verdicts["beta-2-000000000002"].verdict is EnumBatchVerdict.PASSED
    assert [r.batch for r in admission.requests] == [True, True]
    assert sorted(admission.settled) == sorted(verdicts)
    assert admission.released == []
    # the event chain of one item, in order: admission, cost, verdict; then the terminal
    topics = [topic for topic, _ in sink.published]
    chain = [t for t, e in sink.published if getattr(e, "item_id", None) == first_id]
    assert chain == [TOPICS[0], TOPICS[1], TOPICS[2]]
    assert topics[-1] == TOPICS[3]
    assert topics.index(TOPICS[0]) < topics.index(TOPICS[2])


def test_a_refused_item_is_never_submitted_and_reads_degraded(tmp_path: Path) -> None:
    manifest = _store(tmp_path)
    refused = "beta-2-000000000002"
    transport = _Transport()
    admission, sink = _Admission(refuse={refused}), _Sink()

    result = _handler(transport, admission, sink).handle(_request(manifest))

    assert [c.item_id for c in transport.submitted] == ["alpha-1-000000000001"]
    verdict = next(v for v in result.verdicts if v.item_id == refused)
    assert verdict.verdict is EnumBatchVerdict.DEGRADED
    assert verdict.reason == "admission_refused: over the aggregate cap"
    decided = [e for t, e in sink.published if t == TOPICS[0]]
    assert [d.model_dump()["admitted"] for d in decided] == [True, False]
    assert refused not in admission.settled


def test_a_cloud_refused_repository_is_stopped_before_admission(tmp_path: Path) -> None:
    manifest = _store(tmp_path)
    transport, admission, sink = _Transport(), _Admission(), _Sink()

    result = _handler(transport, admission, sink).handle(
        _request(manifest, cloud_refused_repositories=("alpha",))
    )

    assert [r.item_id for r in admission.requests] == ["beta-2-000000000002"]
    verdict = next(v for v in result.verdicts if v.item_id.startswith("alpha"))
    assert verdict.verdict is EnumBatchVerdict.DEGRADED
    assert verdict.reason == "cloud_refused_repository"


def test_a_failed_call_releases_its_reservation(tmp_path: Path) -> None:
    manifest = _store(tmp_path)
    failed = "beta-2-000000000002"
    admission, sink = _Admission(), _Sink()

    result = _handler(_Transport(fail={failed}), admission, sink).handle(
        _request(manifest)
    )

    verdict = next(v for v in result.verdicts if v.item_id == failed)
    assert verdict.verdict is EnumBatchVerdict.DEGRADED
    assert verdict.reason == "upstream_error"
    assert failed in admission.released
    assert failed not in admission.settled


def test_truncated_or_unparseable_output_is_degraded_never_passed(
    tmp_path: Path,
) -> None:
    manifest = _store(tmp_path)
    for stop_reason, content, reason in (
        ("max_tokens", CLEAN, "truncated"),
        ("end_turn", "I could not find anything.", "schema_invalid"),
        ("end_turn", "", "empty_response"),
    ):
        transport = _Transport(
            {"alpha-1-000000000001": content, "beta-2-000000000002": content},
            stop_reason=stop_reason,
        )
        admission = _Admission()
        result = _handler(transport, admission, _Sink()).handle(_request(manifest))
        assert {v.verdict for v in result.verdicts} == {EnumBatchVerdict.DEGRADED}
        assert {v.reason for v in result.verdicts} == {reason}
        # the provider charged for the output, so the reservation settles
        assert len(admission.settled) == 2


def test_a_batch_still_running_holds_its_reservations_and_resumes(
    tmp_path: Path,
) -> None:
    manifest = _store(tmp_path)
    transport = _Transport(ends_after=99)
    admission, sink = _Admission(), _Sink()
    handler = _handler(transport, admission, sink)

    first = handler.handle(_request(manifest, max_polls=2))

    assert first.status == "in_progress"
    assert first.batch_id == "batch-1"
    assert first.verdicts == ()
    assert admission.settled == []
    assert admission.released == []
    assert [t for t, _ in sink.published] == [TOPICS[0], TOPICS[0]]

    transport.ends_after = 1
    second = handler.handle(_request(manifest, resume_batch_id="batch-1", max_polls=2))
    assert second.status == "completed"
    assert len(second.verdicts) == 2
    assert len(transport.submitted) == 2  # resuming submits nothing again
    assert sorted(admission.settled) == sorted(v.item_id for v in second.verdicts)


def test_an_item_the_ended_batch_never_answered_is_released_as_expired(
    tmp_path: Path,
) -> None:
    manifest = _store(tmp_path)

    class _Dropping(_Transport):
        def poll(self, batch_id: str) -> ModelBatchPoll:
            polled = super().poll(batch_id)
            return ModelBatchPoll(ended=True, results=polled.results[:1])

    admission = _Admission()
    result = _handler(_Dropping(), admission, _Sink()).handle(_request(manifest))
    expired = [v for v in result.verdicts if v.reason == "batch_expired"]
    assert len(expired) == 1
    assert expired[0].item_id in admission.released


def test_an_answer_for_an_item_never_submitted_is_ignored(tmp_path: Path) -> None:
    manifest = _store(tmp_path)

    class _ExtraAnswer(_Transport):
        def poll(self, batch_id: str) -> ModelBatchPoll:
            polled = super().poll(batch_id)
            extra = ModelBatchItemResult(
                item_id="not-submitted",
                success=True,
                content=CLEAN,
                usage=ModelBatchUsage(input_tokens=100, output_tokens=20),
            )
            return ModelBatchPoll(ended=True, results=(*polled.results, extra))

    transport, admission = _ExtraAnswer(), _Admission()
    result = _handler(transport, admission, _Sink()).handle(_request(manifest))
    verdict_ids = {v.item_id for v in result.verdicts}
    assert "not-submitted" not in verdict_ids
    assert verdict_ids == {c.item_id for c in transport.submitted}
    assert "not-submitted" not in admission.settled
    assert "not-submitted" not in admission.released


def test_an_item_answered_twice_is_degraded_never_passed(tmp_path: Path) -> None:
    manifest = _store(tmp_path)

    class _Duplicating(_Transport):
        def poll(self, batch_id: str) -> ModelBatchPoll:
            polled = super().poll(batch_id)
            return ModelBatchPoll(
                ended=True, results=(*polled.results, polled.results[0])
            )

    transport, admission = _Duplicating(), _Admission()
    result = _handler(transport, admission, _Sink()).handle(_request(manifest))
    first_id = transport.submitted[0].item_id
    verdicts = [v for v in result.verdicts if v.item_id == first_id]
    assert len(verdicts) == 1
    assert verdicts[0].verdict is EnumBatchVerdict.DEGRADED
    assert verdicts[0].reason == "duplicate_answer"
    assert first_id in admission.settled


def test_a_diff_that_no_longer_matches_the_manifest_is_refused(tmp_path: Path) -> None:
    manifest = _store(tmp_path)
    next((manifest.parent / "diffs").glob("alpha*.diff")).write_text(
        "tampered", encoding="utf-8"
    )
    transport = _Transport()
    with pytest.raises(ValueError, match="no longer matches"):
        _handler(transport, _Admission(), _Sink()).handle(_request(manifest))
    assert transport.submitted == []


def test_item_ids_select_a_subset(tmp_path: Path) -> None:
    manifest = _store(tmp_path)
    transport = _Transport()
    result = _handler(transport, _Admission(), _Sink()).handle(
        _request(manifest, item_ids=("beta-2-000000000002",))
    )
    assert [v.item_id for v in result.verdicts] == ["beta-2-000000000002"]
    with pytest.raises(ValueError, match="not in the manifest"):
        _handler(_Transport(), _Admission(), _Sink()).handle(
            _request(manifest, item_ids=("nope",))
        )


def test_a_synchronous_transport_is_admitted_at_full_price(tmp_path: Path) -> None:
    manifest = _store(tmp_path)
    admission = _Admission()
    result = _handler(_Transport(mode="synchronous"), admission, _Sink()).handle(
        _request(manifest)
    )
    assert [r.batch for r in admission.requests] == [False, False]
    assert {v.execution_mode for v in result.verdicts} == {"synchronous"}


def test_the_stub_admission_reserves_and_refuses_over_its_ceiling() -> None:
    stub = StubReviewAdmission(ceiling_tokens=1000)

    def ask(item: str, tokens: int) -> ModelReviewAdmissionDecision:
        return stub.admit(
            ModelReviewAdmissionRequest(
                item_id=item,
                model_id="m",
                worst_case_input_tokens=tokens,
                max_tokens=100,
                batch=True,
            )
        )

    assert ask("a", 500).admitted is True
    assert ask("a", 500).admitted is True  # the same item id reserves once
    assert ask("b", 500).admitted is False  # 600 + 600 > 1000
    stub.release("a")
    assert ask("b", 500).admitted is True
    stub.settle("b", ModelBatchUsage(input_tokens=1, output_tokens=1))
    assert ask("c", 800).admitted is True  # b settled at 2 tokens


def test_the_delegation_transport_places_each_call_and_maps_the_answer(
    tmp_path: Path,
) -> None:
    from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
    from omnimarket.nodes.node_claude_review_batch_effect.handlers.adapter_delegation_call_transport import (
        DelegationCallTransport,
    )

    class _Outcome:
        def __init__(self, ok: bool, finish: EnumProviderFinishReason) -> None:
            self.success = ok
            self.content = CLEAN if ok else None
            self.tokens_in = 11
            self.tokens_out = 7
            self.finish_reason = finish
            self.failure_class = None if ok else "provider_timeout"

    seen: list[Any] = []

    def call(request: object) -> _Outcome:
        seen.append(request)
        return (
            _Outcome(False, EnumProviderFinishReason.ABSENT)
            if len(seen) == 2
            else _Outcome(True, EnumProviderFinishReason.LENGTH)
        )

    transport = DelegationCallTransport(call)
    calls = [
        ModelBatchCall(
            item_id=f"i{n}",
            model_id="m",
            endpoint_ref="http://voter.invalid/v1/chat/completions",
            secret_ref=None,
            provider="local",
            model_tier="local",
            temperature=0.2,
            system_prompt="sys",
            prompt=f"diff {n}",
            max_tokens=100,
        )
        for n in (1, 2)
    ]
    batch_id = transport.submit(calls)
    poll = transport.poll(batch_id)

    assert transport.execution_mode == "synchronous"
    assert poll.ended is True
    first, second = poll.results
    assert (first.success, first.stop_reason, first.usage.input_tokens) == (
        True,
        "length",
        11,
    )
    assert (second.success, second.failure) == (False, "provider_timeout")
    assert [r.model_tier for r in seen] == ["local", "local"]


def test_the_contract_publishes_the_four_events_in_chain_order() -> None:
    assert TOPICS == [
        "onex.evt.omnimarket.review-call-admission-decided.v1",
        "onex.evt.omnimarket.claude-review-batch-item-cost-recorded.v1",
        "onex.evt.omnimarket.claude-review-batch-item-verdict-recorded.v1",
        "onex.evt.omnimarket.claude-review-batch-completed.v1",
    ]
    assert CONTRACT["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.claude-review-batch-requested.v1"
    )


def test_the_runtime_builds_the_handler_with_no_arguments_and_it_refuses_every_item(
    tmp_path: Path,
) -> None:
    manifest = _store(tmp_path)
    transport_calls = _Transport()
    handler = HandlerClaudeReviewBatch()
    result = handler.handle(_request(manifest))
    assert result.submitted == 0
    assert result.refused == 2
    assert {v.verdict for v in result.verdicts} == {EnumBatchVerdict.DEGRADED}
    assert {v.reason for v in result.verdicts} == {
        "admission_refused: no admission is wired"
    }
    assert transport_calls.submitted == []


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: Any = None,
    ) -> None:
        self.published.append((topic, key, value))

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: Callable[[Any], Awaitable[None]] | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        raise NotImplementedError


class _SinkEvent(BaseModel):
    item_id: str


def test_bus_event_sink_publishes_from_a_worker_thread() -> None:
    async def run() -> None:
        bus = _RecordingBus()
        correlation = uuid.uuid4()
        sink = BusEventSink(bus, asyncio.get_running_loop(), correlation)
        event = _SinkEvent(item_id="item-1")

        await asyncio.to_thread(sink.publish, TOPICS[0], event)

        assert len(bus.published) == 1
        topic, key, value = bus.published[0]
        assert topic == TOPICS[0]
        assert key == str(correlation).encode()
        envelope = json.loads(value)
        assert envelope["correlation_id"] == str(correlation)
        assert envelope["payload"] == event.model_dump(mode="json")

    asyncio.run(run())


def test_bus_event_sink_refuses_a_publish_on_the_loop_thread() -> None:
    async def run() -> None:
        bus = _RecordingBus()
        sink = BusEventSink(bus, asyncio.get_running_loop(), uuid.uuid4())

        with pytest.raises(RuntimeError, match="event loop's thread"):
            sink.publish(TOPICS[0], _SinkEvent(item_id="item-1"))

        assert bus.published == []

    asyncio.run(run())
