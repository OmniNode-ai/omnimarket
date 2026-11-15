# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_review_gate_context_effect (OMN-24614): the golden chain, error chains and injected breaks.

Golden chain: a failure decision for a head with no run posts a failure check run and the chain holds start then
success with counters {"posted": 1}; a success decision posts success; a repeat of the same decision posts nothing
(UNCHANGED, one run only, chain start+success {"unchanged":1}); a verdict that flips posts a success run and a read
of the head then shows success as the newest. NEW PUSH: a run named the same on an OLD head does not satisfy the
new head, so the new head gets its own post. ERROR chains: reader down -> failed_read; poster down -> failed_post;
drop_posts -> failed_readback. Three INJECTED BREAKS: a reader that lists runs of every head, a reader that ignores
the check name, and a poster that posts success when the decision says failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.model_review_gate_context import (
    EnumReviewContextReason,
    EnumReviewContextState,
    ModelReviewGateContextDecision,
)
from omnimarket.nodes.node_review_gate_context_effect.adapters.inmemory_head_check_runs import (
    InMemoryHeadCheckRuns,
)
from omnimarket.nodes.node_review_gate_context_effect.handlers.handler_review_gate_context_effect import (
    HandlerReviewGateContextEffect,
)
from omnimarket.nodes.node_review_gate_context_effect.models.model_review_context_post import (
    EnumReviewContextPostOutcome,
    ModelHeadCheckRun,
    ModelReviewContextPostCommand,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_check_runs import (
    ReviewContextPortError,
)

pytestmark = pytest.mark.unit

HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
REPO = "example-org/example-repo"
CHECK = "review-verdict / clean-head"
NOW = datetime(2026, 11, 8, 13, 0, 0, tzinfo=UTC)


def decision(
    state: EnumReviewContextState = EnumReviewContextState.FAILURE,
    head: str = HEAD,
) -> ModelReviewGateContextDecision:
    reason = (
        EnumReviewContextReason.CLEAN_ON_HEAD
        if state is EnumReviewContextState.SUCCESS
        else EnumReviewContextReason.REVIEW_CHANGES_NEEDED
    )
    return ModelReviewGateContextDecision(
        repository=REPO,
        pr_number=42,
        head_sha=head,
        check_name=CHECK,
        state=state,
        reason=reason,
        bypass_class=None,
        title="Review verdict",
        summary="The review gate context verdict for this head.",
    )


def command(
    state: EnumReviewContextState = EnumReviewContextState.FAILURE,
    head: str = HEAD,
    cid: str = "cid-1",
) -> ModelReviewContextPostCommand:
    return ModelReviewContextPostCommand(
        correlation_id=cid, decision=decision(state, head)
    )


def make_handler(
    reader: object = None,
    poster: object = None,
    sink: InMemoryEmissionSink | None = None,
) -> tuple[HandlerReviewGateContextEffect, InMemoryEmissionSink, InMemoryHeadCheckRuns]:
    sink = sink or InMemoryEmissionSink()
    adapter: InMemoryHeadCheckRuns
    if isinstance(reader, InMemoryHeadCheckRuns):
        adapter = reader
    elif isinstance(poster, InMemoryHeadCheckRuns):
        adapter = poster
    else:
        adapter = InMemoryHeadCheckRuns()
    if reader is None:
        reader = adapter
    if poster is None:
        poster = adapter
    handler = HandlerReviewGateContextEffect(
        reader=reader,
        poster=poster,
        sink=sink,
        emission_node="node_review_gate_context_effect",
        emission_piece=8,
        emission_backend="inmemory",
        route_backend="inmemory",
        clock=lambda: NOW,
    )
    return handler, sink, adapter


def test_golden_failure_post() -> None:
    handler, sink, adapter = make_handler()
    result = handler.handle(command(EnumReviewContextState.FAILURE))
    assert result.outcome is EnumReviewContextPostOutcome.POSTED
    assert result.check_run_id is not None
    assert result.calls == 3
    assert len(sink.events) == 2
    assert sink.events[0].phase == "start"
    assert sink.events[1].phase == "success"
    assert sink.events[1].counters == {"posted": 1}
    assert adapter.posted[0]["title"] == "Review verdict"
    assert adapter.posted[0]["conclusion"] == "failure"
    assert adapter.posted[0]["head_sha"] == HEAD


def test_golden_success_post() -> None:
    handler, sink, adapter = make_handler()
    result = handler.handle(command(EnumReviewContextState.SUCCESS))
    assert result.outcome is EnumReviewContextPostOutcome.POSTED
    assert result.check_run_id is not None
    assert result.calls == 3
    assert len(sink.events) == 2
    assert sink.events[1].counters == {"posted": 1}
    assert adapter.posted[0]["conclusion"] == "success"


def test_repeat_unchanged() -> None:
    handler, sink, adapter = make_handler()
    first = handler.handle(command(EnumReviewContextState.FAILURE))
    assert first.outcome is EnumReviewContextPostOutcome.POSTED
    second = handler.handle(command(EnumReviewContextState.FAILURE, cid="cid-2"))
    assert second.outcome is EnumReviewContextPostOutcome.UNCHANGED
    assert second.calls == 1
    assert second.check_run_id is None
    assert len(sink.events) == 4
    assert sink.events[2].phase == "start"
    assert sink.events[3].phase == "success"
    assert sink.events[3].counters == {"unchanged": 1}
    assert len(adapter.posted) == 1


def test_verdict_flips() -> None:
    handler, _, adapter = make_handler()
    first = handler.handle(command(EnumReviewContextState.FAILURE))
    assert first.outcome is EnumReviewContextPostOutcome.POSTED
    second = handler.handle(command(EnumReviewContextState.SUCCESS, cid="cid-2"))
    assert second.outcome is EnumReviewContextPostOutcome.POSTED
    assert len(adapter.posted) == 2
    runs = adapter.read(repository=REPO, head_sha=HEAD, check_name=CHECK)
    newest = max(runs, key=lambda r: r.check_run_id)
    assert newest.conclusion == "success"


def test_new_push_old_head_not_counted() -> None:
    handler, _, adapter = make_handler()
    old_run = ModelHeadCheckRun(
        check_run_id=1,
        name=CHECK,
        head_sha=OLD_HEAD,
        status="completed",
        conclusion="failure",
    )
    adapter._runs.append(old_run)
    result = handler.handle(command(EnumReviewContextState.FAILURE, head=HEAD))
    assert result.outcome is EnumReviewContextPostOutcome.POSTED
    assert result.calls == 3
    assert len(adapter.posted) == 1
    assert adapter.posted[0]["head_sha"] == HEAD
    runs = adapter.read(repository=REPO, head_sha=HEAD, check_name=CHECK)
    assert max(runs, key=lambda r: r.check_run_id).conclusion == "failure"


def test_reader_down() -> None:
    class DownReader:
        def read(
            self, *, repository: str, head_sha: str, check_name: str
        ) -> tuple[ModelHeadCheckRun, ...]:
            raise ReviewContextPortError("injected read failure")

    handler, sink, _ = make_handler(reader=DownReader())
    result = handler.handle(command())
    assert result.outcome is EnumReviewContextPostOutcome.FAILED_READ
    assert result.calls == 1
    assert len(sink.events) == 2
    assert sink.events[1].phase == "failure"
    assert sink.events[1].failure_code == "review_context_read_failed"


def test_poster_down() -> None:
    class DownPoster:
        def post(
            self,
            *,
            correlation_id: str,
            repository: str,
            head_sha: str,
            check_name: str,
            conclusion: str,
            title: str,
            summary: str,
        ) -> int:
            raise ReviewContextPortError("injected post failure")

    handler, sink, _ = make_handler(poster=DownPoster())
    result = handler.handle(command())
    assert result.outcome is EnumReviewContextPostOutcome.FAILED_POST
    assert result.calls == 2
    assert len(sink.events) == 2
    assert sink.events[1].phase == "failure"
    assert sink.events[1].failure_code == "review_context_post_failed"


def test_drop_posts_readback_mismatch() -> None:
    adapter = InMemoryHeadCheckRuns(drop_posts=True)
    handler, sink, _ = make_handler(reader=adapter, poster=adapter)
    result = handler.handle(command())
    assert result.outcome is EnumReviewContextPostOutcome.FAILED_READBACK
    assert result.calls == 3
    assert len(sink.events) == 2
    assert sink.events[1].phase == "failure"
    assert sink.events[1].failure_code == "review_context_readback_mismatch"


def test_injected_break_reader_lists_every_head() -> None:
    class LeakyReader:
        """Lists the runs of every head, ignoring the head asked for."""

        def __init__(self, inner: InMemoryHeadCheckRuns) -> None:
            self._inner = inner

        def read(
            self, *, repository: str, head_sha: str, check_name: str
        ) -> tuple[ModelHeadCheckRun, ...]:
            return tuple(self._inner._runs)

    adapter = InMemoryHeadCheckRuns()
    old_run = ModelHeadCheckRun(
        check_run_id=1,
        name=CHECK,
        head_sha=OLD_HEAD,
        status="completed",
        conclusion="failure",
    )
    adapter._runs.append(old_run)
    handler, _, _ = make_handler(reader=LeakyReader(adapter), poster=adapter)
    result = handler.handle(command(EnumReviewContextState.FAILURE, head=HEAD))
    assert result.outcome is EnumReviewContextPostOutcome.POSTED
    assert len(adapter.posted) == 1
    assert adapter.posted[0]["head_sha"] == HEAD


def test_injected_break_reader_ignores_check_name() -> None:
    class NameBlindReader:
        """Lists the runs of the head, ignoring the check name asked for."""

        def __init__(self, inner: InMemoryHeadCheckRuns) -> None:
            self._inner = inner

        def read(
            self, *, repository: str, head_sha: str, check_name: str
        ) -> tuple[ModelHeadCheckRun, ...]:
            return tuple(r for r in self._inner._runs if r.head_sha == head_sha)

    adapter = InMemoryHeadCheckRuns()
    other_run = ModelHeadCheckRun(
        check_run_id=1,
        name="other-check",
        head_sha=HEAD,
        status="completed",
        conclusion="success",
    )
    adapter._runs.append(other_run)
    handler, _, _ = make_handler(reader=NameBlindReader(adapter), poster=adapter)
    result = handler.handle(command(EnumReviewContextState.SUCCESS))
    assert result.outcome is EnumReviewContextPostOutcome.POSTED
    assert len(adapter.posted) == 1
    assert adapter.posted[0]["conclusion"] == "success"


def test_injected_break_poster_posts_wrong_conclusion() -> None:
    class WrongPoster:
        """Posts success even when the decision says failure; the readback catches it."""

        def __init__(self, inner: InMemoryHeadCheckRuns) -> None:
            self._inner = inner

        def post(
            self,
            *,
            correlation_id: str,
            repository: str,
            head_sha: str,
            check_name: str,
            conclusion: str,
            title: str,
            summary: str,
        ) -> int:
            return self._inner.post(
                correlation_id=correlation_id,
                repository=repository,
                head_sha=head_sha,
                check_name=check_name,
                conclusion="success",
                title=title,
                summary=summary,
            )

    adapter = InMemoryHeadCheckRuns()
    handler, sink, _ = make_handler(reader=adapter, poster=WrongPoster(adapter))
    result = handler.handle(command(EnumReviewContextState.FAILURE))
    assert result.outcome is EnumReviewContextPostOutcome.FAILED_READBACK
    assert result.calls == 3
    assert sink.events[1].failure_code == "review_context_readback_mismatch"
