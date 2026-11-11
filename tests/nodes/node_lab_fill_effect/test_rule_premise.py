# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Send-time premise regression: an unchecked unmerged dependency cannot launch."""

from pathlib import Path

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_launch import (
    HandlerLabFillLaunch,
)

from .fakes import FakeBlocks, FakeChecks, FakeClock, FakeRunner
from .test_error_chain_lab_fill_effect import _launch_request


def test_rule_premise_unmerged_brief_is_not_sent(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = HandlerLabFillLaunch(
        checks=FakeChecks(), blocks=FakeBlocks(), runner=runner, clock=FakeClock()
    ).handle(
        _launch_request(
            tmp_path,
            brief="Build using repo#9, whose pull request is still unmerged.\n",
        )
    )
    assert not result.detached
    assert result.skipped == "brief-check-refused"
    assert runner.calls == []


def test_rule_premise_failed_check_is_not_sent(tmp_path: Path) -> None:
    from omnimarket.models.model_rule_premise import (
        ModelBriefCheckRequest,
        ModelBriefCheckResult,
    )

    class FailedGate:
        def handle(self, request: ModelBriefCheckRequest) -> ModelBriefCheckResult:
            return ModelBriefCheckResult(
                verdict="refuse", reason="live premise check failed"
            )

    runner = FakeRunner()
    result = HandlerLabFillLaunch(
        checks=FakeChecks(),
        blocks=FakeBlocks(),
        runner=runner,
        clock=FakeClock(),
        premise_gate=FailedGate(),
    ).handle(_launch_request(tmp_path))
    assert result.skipped == "brief-check-refused"
    assert runner.calls == []


def test_rule_premise_passing_verdict_without_receipt_is_not_sent(
    tmp_path: Path,
) -> None:
    from omnimarket.models.model_rule_premise import (
        ModelBriefCheckRequest,
        ModelBriefCheckResult,
    )

    class UnstampedGate:
        def handle(self, request: ModelBriefCheckRequest) -> ModelBriefCheckResult:
            return ModelBriefCheckResult(verdict="pass")

    runner = FakeRunner()
    result = HandlerLabFillLaunch(
        checks=FakeChecks(),
        blocks=FakeBlocks(),
        runner=runner,
        clock=FakeClock(),
        premise_gate=UnstampedGate(),
    ).handle(_launch_request(tmp_path))
    assert result.skipped == "brief-check-refused"
    assert runner.calls == []


def test_rule_premise_passing_receipt_is_for_the_exact_sent_bytes(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_rule_premise_effect import brief_receipt

    from .fakes import FakePremiseGate

    runner = FakeRunner()
    result = HandlerLabFillLaunch(
        checks=FakeChecks(),
        blocks=FakeBlocks(),
        runner=runner,
        clock=FakeClock(),
        premise_gate=FakePremiseGate(),
    ).handle(_launch_request(tmp_path))
    assert result.detached
    assert len(runner.calls) == 1
    receipt = runner.calls[0][1]["ONEX_BRIEF_CHECK_RECEIPT"]
    assert receipt == brief_receipt((tmp_path / "lab-fill-omn7-x.md").read_text())


def test_rule_premise_lab_tick_status_names_refused_ticket(tmp_path: Path) -> None:
    from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillCommandOutcome

    from .test_golden_chain_lab_fill_tick import _request, _row, _tick

    runner = FakeRunner(
        outcome=LabFillCommandOutcome(2, "", "REMOTE-LANE-BRIEF-UNCHECKED REFUSED")
    )
    handler, _, _ = _tick([_row(1)], runner=runner)
    result = handler.handle(_request(tmp_path))
    assert "refused=1 tickets=OMN-1" in result.status_line
    assert "lab-fill-brief" not in result.status_line
    assert result.refusals[0].reason == "brief-check-refused"


def test_rule_premise_receipt_without_batch_admission_does_not_send(
    tmp_path: Path,
) -> None:
    from omnimarket.models.model_rule_premise import (
        ModelBriefCheckRequest,
        ModelBriefCheckResult,
    )
    from omnimarket.nodes.node_rule_premise_effect import brief_receipt

    class UnadmittedGate:
        def handle(self, request: ModelBriefCheckRequest) -> ModelBriefCheckResult:
            return ModelBriefCheckResult(
                verdict="pass", receipt=brief_receipt(request.brief)
            )

    runner = FakeRunner()
    result = HandlerLabFillLaunch(
        checks=FakeChecks(),
        blocks=FakeBlocks(),
        runner=runner,
        clock=FakeClock(),
        premise_gate=UnadmittedGate(),
    ).handle(_launch_request(tmp_path))
    assert result.skipped == "brief-check-refused"
    assert runner.calls == []
