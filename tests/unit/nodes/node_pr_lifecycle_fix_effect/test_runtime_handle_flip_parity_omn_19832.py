# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity tests for the canonical-shape flip of node_pr_lifecycle_fix_effect.

OMN-19832 turns the contract-bound ``HandlerPrLifecycleFixRuntime`` from a
subclass that only inherited ``handle`` into a wrapper that defines its own
``handle(command) -> ModelHandlerOutput``. The OMN-14355 ratchet reads that as a
baselined node flipping to canonical, so the hand-flip proof under
``scripts/ci/adequacy_receipts/`` names these tests as its parity ids. The flip
bundle gate runs them twice: they must pass on this tree and fail on their own
assertion on the receipt's ``base_ref``, where the runtime handler still returned
the bare result.

Each test imports only symbols that exist on both trees and injects its fakes
through module attributes, not through constructor parameters the base tree
does not have, so a failure on the base tree is an assertion and never an import
or a ``TypeError``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionOp,
    EnumPrLandingCompanionOutcomeKind,
    ModelPrLandingCompanionOutcome,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
    handler_pr_lifecycle_fix as fix_module,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
    HandlerPrLifecycleFixRuntime,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelPrLifecycleFixResult,
)

_REPO = "OmniNode-ai/omnimarket"
_HEAD = "b" * 40
_AUTHORED = (
    "authored OCC companion Evidence-Source: OCC#11400 for OMN-19832 on "
    f"{_REPO}#2990 (product head {_HEAD}, branch "
    "auto/omninode-ai-omnimarket-pr-2990-occ-autobind)"
)


def _command(
    block_reason: EnumPrBlockReason,
    op: EnumPrLandingCompanionOp = EnumPrLandingCompanionOp.DERIVE,
) -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=UUID("22222222-3333-4444-5555-666666666666"),
        pr_number=2990,
        repo=_REPO,
        block_reason=block_reason,
        ticket_id="OMN-19832",
        op=op,
        requested_at=datetime(2026, 9, 27, 7, 0, tzinfo=UTC),
    )


class _AutobindAdapter:
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
        return _AUTHORED


class _Reporter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


@pytest.fixture
def reporter(monkeypatch: pytest.MonkeyPatch) -> _Reporter:
    """No network: fake the marker reporter, the token and the head read."""
    recording = _Reporter()
    monkeypatch.setattr(fix_module, "report_autobind_outcome", recording)
    monkeypatch.setattr(
        fix_module, "_default_outcome_token_resolver", lambda: "ghs_test_token"
    )
    monkeypatch.setattr(
        fix_module,
        "resolve_product_head_sha",
        lambda **_kwargs: _HEAD,
        raising=False,
    )
    return recording


def _comparable(result: ModelPrLifecycleFixResult) -> dict[str, object]:
    return result.model_dump(exclude={"completed_at"})


@pytest.mark.unit
def test_the_contract_bound_handler_defines_its_own_handle() -> None:
    """The flip itself: the classifier can now see the handler's entry point."""
    assert "handle" in vars(HandlerPrLifecycleFixRuntime), (
        "HandlerPrLifecycleFixRuntime only inherits handle()"
    )


@pytest.mark.unit
async def test_a_non_companion_command_yields_the_unchanged_result_as_its_only_event(
    reporter: _Reporter,
) -> None:
    command = _command(EnumPrBlockReason.CI_FAILURE)
    output = await HandlerPrLifecycleFixRuntime().handle(command)

    assert isinstance(output, ModelHandlerOutput), type(output).__name__
    assert len(output.events) == 1
    (event,) = output.events
    assert isinstance(event, ModelPrLifecycleFixResult)
    expected = await HandlerPrLifecycleFix().handle(command)
    assert _comparable(event) == _comparable(expected)
    assert output.correlation_id == command.correlation_id
    assert reporter.calls == []


@pytest.mark.unit
async def test_an_autobind_command_yields_the_unchanged_result_then_the_typed_outcome(
    reporter: _Reporter,
) -> None:
    command = _command(
        EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
        EnumPrLandingCompanionOp.REGENERATE,
    )
    output = await HandlerPrLifecycleFixRuntime(
        occ_autobind_adapter=_AutobindAdapter()
    ).handle(command)

    assert isinstance(output, ModelHandlerOutput), type(output).__name__
    assert [type(e) for e in output.events] == [
        ModelPrLifecycleFixResult,
        ModelPrLandingCompanionOutcome,
    ]
    result, outcome = output.events
    assert isinstance(result, ModelPrLifecycleFixResult)
    assert isinstance(outcome, ModelPrLandingCompanionOutcome)
    expected = await HandlerPrLifecycleFix(
        occ_autobind_adapter=_AutobindAdapter()
    ).handle(command)
    assert _comparable(result) == _comparable(expected)
    assert outcome.kind is EnumPrLandingCompanionOutcomeKind.MINTED
    assert outcome.op is EnumPrLandingCompanionOp.REGENERATE
    assert outcome.head_sha == _HEAD
    assert outcome.occ_pr == 11400
    # One marker from the runtime run and one from the reference run.
    assert len(reporter.calls) == 2
    assert reporter.calls[0]["head_sha"] == _HEAD
