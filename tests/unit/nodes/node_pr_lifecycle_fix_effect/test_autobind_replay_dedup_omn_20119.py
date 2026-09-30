# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A replayed copy of a completed autobind command is answered, not re-run (OMN-20119).

``onex.cmd.omnimarket.occ-autobind.v1`` has two consumer groups: this producer
(effects runtime) and the PR landing orchestrator (main runtime). When the
orchestrator failed a message, the dead-letter replay wrote it back to the
shared topic five times, and each copy re-ran the whole autobind here: the
same correlation id, the same request, another companion authoring pass and
another check-run marker on the product PR. On the .201 dev lane on
2026-09-30 one publish for omnibase_infra#4321 became 21 copies, and 239
queued messages carried only 29 distinct requests.

The runtime handler now remembers the commands it completed. A byte-identical
copy (same correlation id, command id, op, repository and PR) gets the answer
it already produced; a new request (a fresh correlation id) runs as before.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from omnimarket.events.pr_landing_companion import EnumPrLandingCompanionOp
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
    HandlerPrLifecycleFixRuntime,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnibase_infra"
_HEAD = "b" * 40
_NO_RED = (
    "skip:NO_RED_DERIVABLE_CHECK — OmniNode-ai/omnibase_infra#4321: no changed-file "
    "candidate is RED-derivable against the merge base"
)


def _command(
    correlation: str = "d2bde377-766f-4c61-8fe2-11134890512d",
    *,
    block_reason: EnumPrBlockReason = EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
) -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=UUID(correlation),
        pr_number=4321,
        repo=_REPO,
        block_reason=block_reason,
        ticket_id="OMN-20112",
        op=EnumPrLandingCompanionOp.DERIVE,
        requested_at=datetime(2026, 9, 30, 1, 0, tzinfo=UTC),
    )


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    async def autobind_evidence_source(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str | None = None,
        *,
        batch_mode: object = None,
        op: object = None,
    ) -> str:
        del repo, pr_number, ticket_id, batch_mode, op
        self.calls += 1
        return _NO_RED


class _Reporter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs: Any) -> bool:
        del kwargs
        self.calls += 1
        return True


@pytest.fixture
def reporter(monkeypatch: pytest.MonkeyPatch) -> _Reporter:
    recording = _Reporter()
    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
        "handler_pr_lifecycle_fix.report_autobind_outcome",
        recording,
    )
    return recording


def _runtime(adapter: _Adapter) -> HandlerPrLifecycleFixRuntime:
    return HandlerPrLifecycleFixRuntime(
        occ_autobind_adapter=adapter,
        outcome_token_resolver=lambda: "ghs_test_token",
        head_sha_resolver=lambda *_args: _HEAD,
    )


async def test_a_replayed_copy_is_answered_without_a_second_run(
    reporter: _Reporter, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _Adapter()
    runtime = _runtime(adapter)
    first = await runtime.handle(_command())
    with caplog.at_level(logging.WARNING):
        copies = [await runtime.handle(_command()) for _ in range(5)]
    assert adapter.calls == 1
    assert reporter.calls == 1
    for copy in copies:
        assert copy.events == first.events
    assert "duplicate delivery" in caplog.text
    assert "4321" in caplog.text


async def test_a_fresh_request_for_the_same_pr_runs_again(reporter: _Reporter) -> None:
    adapter = _Adapter()
    runtime = _runtime(adapter)
    await runtime.handle(_command())
    await runtime.handle(_command("cdc37dda-0000-4000-8000-000000000001"))
    assert adapter.calls == 2
    assert reporter.calls == 2


async def test_a_run_that_raised_is_not_remembered(
    reporter: _Reporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter()
    runtime = _runtime(adapter)
    boom = RuntimeError("transient")

    async def _raise(command: ModelPrLifecycleFixCommand) -> Any:
        del command
        raise boom

    real = runtime.fix_handler.handle_with_companion_outcome
    monkeypatch.setattr(runtime.fix_handler, "handle_with_companion_outcome", _raise)
    with pytest.raises(RuntimeError):
        await runtime.handle(_command())
    monkeypatch.setattr(runtime.fix_handler, "handle_with_companion_outcome", real)
    await runtime.handle(_command())
    assert adapter.calls == 1


async def test_the_memory_is_bounded(reporter: _Reporter) -> None:
    adapter = _Adapter()
    runtime = _runtime(adapter)
    first = "00000000-0000-4000-8000-000000000000"
    await runtime.handle(_command(first))
    for n in range(1, HandlerPrLifecycleFixRuntime.COMPLETED_MEMORY + 1):
        await runtime.handle(_command(f"00000000-0000-4000-8000-{n:012d}"))
    calls = adapter.calls
    # The oldest entry was evicted, so its copy runs again.
    await runtime.handle(_command(first))
    assert adapter.calls == calls + 1


async def test_a_run_that_errored_is_run_again_on_a_copy(reporter: _Reporter) -> None:
    class _Raising(_Adapter):
        async def autobind_evidence_source(
            self,
            repo: str,
            pr_number: int,
            ticket_id: str | None = None,
            *,
            batch_mode: object = None,
            op: object = None,
        ) -> str:
            del repo, pr_number, ticket_id, batch_mode, op
            self.calls += 1
            raise RuntimeError("GitHub 422")

    adapter = _Raising()
    runtime = _runtime(adapter)
    await runtime.handle(_command())
    await runtime.handle(_command())
    assert adapter.calls == 2
