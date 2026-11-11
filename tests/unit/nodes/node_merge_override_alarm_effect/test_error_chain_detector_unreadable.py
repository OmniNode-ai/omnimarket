# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: an unreadable source is detector_unreadable with the same alarm as a finding."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.events.pr_landing_github.enum_pr_landing_github_failure_reason import (
    EnumPrLandingGithubFailureReason,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_failed import (
    ModelPrLandingGithubFailed,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    EnumMergeOverrideVerdict,
    ModelMergeOverrideSweepTrigger,
)
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    FINDING_SUFFIX,
    HEAD,
    PR,
    READ_SUFFIX,
    REPO,
    SETTINGS,
    SLACK_SUFFIX,
    Publisher,
    Rig,
    drive_merge,
    make_rig,
    pr_merged_event,
    read_answer,
)

pytestmark = pytest.mark.unit
UNREADABLE = EnumMergeOverrideVerdict.DETECTOR_UNREADABLE


def _one_alarm(rig: Rig, key_tail: str) -> None:
    [finding] = rig.publisher.on(FINDING_SUFFIX)
    assert finding["verdict"] == "detector_unreadable"
    assert finding["idempotency_key"] == f"merge-override:{REPO}#{PR}:{key_tail}"
    [command] = rig.publisher.on(SLACK_SUFFIX)
    assert command["idempotency_key"] == finding["idempotency_key"]
    [open_row] = rig.rows("merge_override_open_findings")
    assert open_row["verdict"] == "detector_unreadable"


def test_error_chain_unreadable_ledger_is_detector_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, reader_error="ledger projection down")
    result = drive_merge(rig)
    assert result.verdict is not None
    assert result.verdict.verdict is UNREADABLE
    assert "ledger projection down" in (result.verdict.reason or "")
    assert result.verdict.head_sha == HEAD
    _one_alarm(rig, HEAD)


def test_error_chain_failed_github_read_is_detector_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.handler.handle(pr_merged_event())
    correlation = UUID(str(rig.publisher.on(READ_SUFFIX)[-1]["correlation_id"]))
    failed = ModelPrLandingGithubFailed(
        correlation_id=correlation,
        operation=EnumPrLandingGithubOperation.READ_PR_STATE,
        mode=EnumPrLandingGithubMode.ENFORCE,
        repository=REPO,
        pr_number=PR,
        reason=EnumPrLandingGithubFailureReason.TRANSPORT_ERROR,
        detail="connection reset",
        http_status=None,
        quota=None,
    )
    result = rig.handler.handle(failed)
    assert result.verdict is not None
    assert result.verdict.verdict is UNREADABLE
    assert "transport_error" in (result.verdict.reason or "")
    _one_alarm(rig, "unreadable")


@pytest.mark.parametrize("kind", ["not_merged", "not_modified"])
def test_error_chain_read_that_does_not_show_a_merge_is_detector_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    from tests.unit.nodes.node_merge_override_alarm_effect.support import pr_state

    rig = make_rig(monkeypatch, tmp_path)
    rig.handler.handle(pr_merged_event())
    answer = (
        read_answer(rig, state=pr_state(merged=False))
        if kind == "not_merged"
        else read_answer(rig, not_modified=True)
    )
    result = rig.handler.handle(answer)
    assert result.verdict is not None
    assert result.verdict.verdict is UNREADABLE
    _one_alarm(rig, "unreadable")


def test_error_chain_no_publisher_is_detector_unreadable_and_still_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.handler._publisher = None
    result = rig.handler.handle(pr_merged_event())
    assert result.verdict is not None
    assert result.verdict.verdict is UNREADABLE
    assert result.error
    assert "no event_publisher" in result.error
    [verdict_row] = rig.rows("merge_override_verdicts")
    assert verdict_row["verdict"] == "detector_unreadable"
    [open_row] = rig.rows("merge_override_open_findings")
    assert open_row["verdict"] == "detector_unreadable"


def test_error_chain_a_failing_publish_names_the_step_and_keeps_the_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.handler.handle(pr_merged_event())
    answer = read_answer(rig)
    rig.handler._publisher = Publisher(error="broker down")
    result = rig.handler.handle(answer)
    assert result.verdict is not None
    assert result.command_published is False
    assert result.finding_published is False
    assert result.error
    assert "slack command" in result.error
    assert "broker down" in result.error
    assert len(rig.rows("merge_override_open_findings")) == 1


def test_error_chain_sweep_turns_an_unanswered_read_into_a_finding_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    timeout = int(str(SETTINGS["pending_read_timeout_seconds"]))
    interval = int(str(SETTINGS["sweep_interval_seconds"]))
    rig.handler.handle(pr_merged_event())
    trigger = ModelMergeOverrideSweepTrigger()
    early = rig.handler.handle(trigger)
    assert early.outcome.value == "swept"
    assert early.swept == 0
    assert rig.handler.handle(trigger).outcome.value == "throttled"
    rig.clock.advance(timeout + interval + 1)
    swept = rig.handler.handle(trigger)
    assert swept.outcome.value == "swept"
    assert swept.swept == 1
    _one_alarm(rig, "unreadable")
    rig.clock.advance(interval + 1)
    assert rig.handler.handle(trigger).swept == 0
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1


def test_error_chain_missing_overlay_refuses_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "nowhere"))
    result = rig.handler.handle(pr_merged_event())
    assert result.outcome.value == "refused"
    assert rig.publisher.calls == []
    assert rig.rows("merge_override_pending") == []
