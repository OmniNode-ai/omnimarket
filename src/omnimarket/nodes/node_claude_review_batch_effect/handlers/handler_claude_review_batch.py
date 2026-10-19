# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Submit frozen replay items for review through admission and collect their verdicts (OMN-20910).

Per item: the cloud-refusal check, then admission (batch-priced when the
transport batches), then one submit for the admitted items, then polling, then
for each answer the parse and the verdict. Nothing is passed on silence: a
refused, truncated, unparseable, empty, failed, twice-answered or never-answered
item is ``degraded``, and a reservation is settled when the provider may have
charged and released when it was never dispatched or never answered. An answer
for an item that was never submitted is ignored.

Events are published through the sink as the chain advances, in the order the
plan reads them back: admission decided, cost recorded, verdict recorded, then
the terminal event. The transport and the admission are seams. The shipped
transport runs each item through the delegation call effect (a synchronous
fallback); the provider's batch API joins when the native-messages protocol of
the delegation call effect lands. Admission is the budget reducer when it lands
and a stub until then.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel

from omnimarket.models.model_review_finding import EnumFindingSeverity
from omnimarket.models.review_replay.model_replay_manifest import (
    ModelReplayItem,
    ModelReplayManifest,
)
from omnimarket.nodes.node_claude_review_batch_effect.handlers.stub_review_admission import (
    NullEventSink,
    RefusingReviewAdmission,
)
from omnimarket.nodes.node_claude_review_batch_effect.models import (
    EnumBatchVerdict,
    ModelBatchCall,
    ModelBatchItemResult,
    ModelBatchPoll,
    ModelBatchUsage,
    ModelClaudeReviewBatchRequest,
    ModelClaudeReviewBatchResult,
    ModelReviewAdmissionDecision,
    ModelReviewAdmissionRequest,
    ModelReviewBatchItemCost,
    ModelReviewBatchItemVerdict,
    ModelReviewCallAdmissionDecided,
)
from omnimarket.review.response_parser import EnumParseStatus, parse_model_response

_CONTRACT = Path(__file__).parent.parent / "contract.yaml"
_TRUNCATED = frozenset({"max_tokens", "length"})
_BLOCKING = frozenset({EnumFindingSeverity.CRITICAL, EnumFindingSeverity.MAJOR})


class ProtocolBatchTransport(Protocol):
    """Places review calls and reports their answers."""

    @property
    def execution_mode(self) -> str:
        """``batch`` for a provider batch (half price), ``synchronous`` otherwise."""
        ...

    def submit(self, calls: Sequence[ModelBatchCall]) -> str: ...

    def poll(self, batch_id: str) -> ModelBatchPoll: ...


class ProtocolReviewAdmission(Protocol):
    """The budget reducer's seam: reserve before a call, settle or release after."""

    def admit(
        self, request: ModelReviewAdmissionRequest
    ) -> ModelReviewAdmissionDecision: ...

    def settle(self, item_id: str, usage: ModelBatchUsage) -> None: ...

    def release(self, item_id: str) -> None: ...


class ProtocolEventSink(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...


def event_topics() -> dict[str, str]:
    """Event model name -> topic, from the contract's published events."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return {e["event_type"]: e["topic"] for e in contract["published_events"]}


def _normalize(repo: str) -> str:
    return repo.strip().lower().rsplit("/", 1)[-1].replace("-", "_")


def _worst_case_input_tokens(*texts: str) -> int:
    return sum(len(t.encode("utf-8")) for t in texts) // 3 + 1


class HandlerClaudeReviewBatch:
    """Effect: admission, submit, collect, parse and verdict for replay items."""

    def __init__(
        self,
        transport: ProtocolBatchTransport | None = None,
        admission: ProtocolReviewAdmission | None = None,
        sink: ProtocolEventSink | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wire the seams; the runtime builds this with none, which refuses every item.

        With no admission the budget reducer is absent, so every item is refused
        (``RefusingReviewAdmission``) and nothing is spent. With no transport the
        delegation call effect is the transport, built on first use.
        """
        self._transport_seam = transport
        self._admission = admission or RefusingReviewAdmission()
        self._sink = sink or NullEventSink()
        self._sleep = sleep
        self._topics = event_topics()

    @property
    def _transport(self) -> ProtocolBatchTransport:
        if self._transport_seam is None:
            from omnimarket.nodes.node_claude_review_batch_effect.handlers.adapter_delegation_call_transport import (
                DelegationCallTransport,
            )
            from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
                HandlerLlmDelegationCall,
            )

            self._transport_seam = DelegationCallTransport(HandlerLlmDelegationCall())
        return self._transport_seam

    def _emit(self, event: BaseModel) -> None:
        self._sink.publish(self._topics[type(event).__name__], event)

    def handle(
        self, request: ModelClaudeReviewBatchRequest
    ) -> ModelClaudeReviewBatchResult:
        mode = self._transport.execution_mode
        items = self._load_items(request)
        refused_repos = {_normalize(r) for r in request.cloud_refused_repositories}
        early: list[ModelReviewBatchItemVerdict] = []
        diffs: dict[str, str] = {}
        manifest_dir = Path(request.manifest_path).parent
        for item in items.values():
            diffs[item.item_id] = self._read_diff(manifest_dir, item)

        if request.resume_batch_id is not None:
            batch_id: str | None = request.resume_batch_id
            submitted = list(items)
        else:
            calls: list[ModelBatchCall] = []
            for item in items.values():
                if _normalize(item.repo) in refused_repos:
                    early.append(
                        self._degraded(request, item, mode, "cloud_refused_repository")
                    )
                    continue
                prompt = (
                    f"Repository: {item.repo}\nPull request: #{item.pr_number}\n\n"
                    f"{diffs[item.item_id]}"
                )
                decision = self._admission.admit(
                    ModelReviewAdmissionRequest(
                        item_id=item.item_id,
                        model_id=request.model_id,
                        worst_case_input_tokens=_worst_case_input_tokens(
                            request.system_prompt, prompt
                        ),
                        max_tokens=request.max_tokens,
                        batch=mode == "batch",
                    )
                )
                self._emit(
                    ModelReviewCallAdmissionDecided(
                        correlation_id=request.correlation_id,
                        item_id=item.item_id,
                        model_id=request.model_id,
                        execution_mode=mode,
                        admitted=decision.admitted,
                        reason=decision.reason,
                        reservation_id=decision.reservation_id,
                    )
                )
                if not decision.admitted:
                    early.append(
                        self._degraded(
                            request,
                            item,
                            mode,
                            f"admission_refused: {decision.reason}",
                        )
                    )
                    continue
                calls.append(
                    ModelBatchCall(
                        item_id=item.item_id,
                        model_id=request.model_id,
                        endpoint_ref=request.endpoint_ref,
                        secret_ref=request.secret_ref,
                        provider=request.provider,
                        model_tier=request.model_tier,
                        temperature=request.temperature,
                        system_prompt=request.system_prompt,
                        prompt=prompt,
                        max_tokens=request.max_tokens,
                    )
                )
            batch_id = self._transport.submit(calls) if calls else None
            submitted = [c.item_id for c in calls]

        poll = ModelBatchPoll(ended=batch_id is None)
        for attempt in range(request.max_polls if batch_id is not None else 0):
            poll = self._transport.poll(batch_id or "")
            if poll.ended:
                break
            if attempt + 1 < request.max_polls:
                self._sleep(request.poll_interval_seconds)

        if not poll.ended:
            return ModelClaudeReviewBatchResult(
                correlation_id=request.correlation_id,
                status="in_progress",
                batch_id=batch_id,
                execution_mode=mode,
                submitted=len(submitted),
                refused=len(early),
                verdicts=tuple(early),
                pending_item_ids=tuple(submitted),
            )

        answers: dict[str, ModelBatchItemResult] = {}
        submitted_ids = set(submitted)
        duplicates: set[str] = set()
        for r in poll.results:
            if r.item_id not in submitted_ids:
                continue
            if r.item_id in answers:
                duplicates.add(r.item_id)
            else:
                answers[r.item_id] = r
        verdicts = list(early)
        for item_id in submitted:
            item = items[item_id]
            answer = answers.get(item_id)
            if answer is None:
                self._admission.release(item_id)
                verdicts.append(self._degraded(request, item, mode, "batch_expired"))
            elif item_id in duplicates:
                self._admission.settle(item_id, answer.usage)
                verdicts.append(self._degraded(request, item, mode, "duplicate_answer"))
            else:
                verdicts.append(self._judge(request, item, mode, answer))

        result = ModelClaudeReviewBatchResult(
            correlation_id=request.correlation_id,
            status="completed",
            batch_id=batch_id,
            execution_mode=mode,
            submitted=len(submitted),
            refused=len(early),
            verdicts=tuple(verdicts),
        )
        self._emit(result)
        return result

    def _load_items(
        self, request: ModelClaudeReviewBatchRequest
    ) -> dict[str, ModelReplayItem]:
        manifest = ModelReplayManifest.model_validate_json(
            Path(request.manifest_path).read_text(encoding="utf-8")
        )
        by_id = {i.item_id: i for i in manifest.items}
        wanted = request.item_ids or tuple(by_id)
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise ValueError(f"item(s) not in the manifest: {', '.join(missing)}")
        return {i: by_id[i] for i in wanted}

    @staticmethod
    def _read_diff(manifest_dir: Path, item: ModelReplayItem) -> str:
        body = (manifest_dir / item.diff_file).read_bytes()
        if hashlib.sha256(body).hexdigest() != item.diff_sha256:
            raise ValueError(
                f"diff {item.diff_file} no longer matches the manifest's hash"
            )
        return body.decode("utf-8")

    def _degraded(
        self,
        request: ModelClaudeReviewBatchRequest,
        item: ModelReplayItem,
        mode: str,
        reason: str,
    ) -> ModelReviewBatchItemVerdict:
        verdict = ModelReviewBatchItemVerdict(
            correlation_id=request.correlation_id,
            item_id=item.item_id,
            repo=item.repo,
            pr_number=item.pr_number,
            verdict=EnumBatchVerdict.DEGRADED,
            findings=0,
            blocking_findings=0,
            execution_mode=mode,
            reason=reason,
        )
        self._emit(verdict)
        return verdict

    def _judge(
        self,
        request: ModelClaudeReviewBatchRequest,
        item: ModelReplayItem,
        mode: str,
        answer: ModelBatchItemResult,
    ) -> ModelReviewBatchItemVerdict:
        def cost(outcome: str) -> None:
            self._emit(
                ModelReviewBatchItemCost(
                    correlation_id=request.correlation_id,
                    item_id=item.item_id,
                    model_id=request.model_id,
                    execution_mode=mode,
                    usage=answer.usage,
                    stop_reason=answer.stop_reason,
                    outcome=outcome,
                )
            )

        if not answer.success:
            self._admission.release(item.item_id)
            cost("upstream_error")
            return self._degraded(
                request, item, mode, answer.failure or "upstream_error"
            )

        # The provider answered, so it may have charged: settle at the measured usage.
        self._admission.settle(item.item_id, answer.usage)
        content = answer.content or ""
        if answer.stop_reason in _TRUNCATED:
            cost("truncated")
            return self._degraded(request, item, mode, "truncated")
        if not content.strip():
            cost("schema_invalid")
            return self._degraded(request, item, mode, "empty_response")
        parsed = parse_model_response(content, source_model=request.model_id)
        if parsed.status is not EnumParseStatus.SUCCESS:
            cost("schema_invalid")
            return self._degraded(request, item, mode, "schema_invalid")
        cost("ok")
        blocking = sum(f.severity in _BLOCKING for f in parsed.findings)
        verdict = ModelReviewBatchItemVerdict(
            correlation_id=request.correlation_id,
            item_id=item.item_id,
            repo=item.repo,
            pr_number=item.pr_number,
            verdict=EnumBatchVerdict.BLOCKED if blocking else EnumBatchVerdict.PASSED,
            findings=len(parsed.findings),
            blocking_findings=blocking,
            execution_mode=mode,
        )
        self._emit(verdict)
        return verdict


__all__ = [
    "HandlerClaudeReviewBatch",
    "ProtocolBatchTransport",
    "ProtocolEventSink",
    "ProtocolReviewAdmission",
    "event_topics",
]
