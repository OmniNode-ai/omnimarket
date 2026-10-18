# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner and dev-head classes are decided from the event, not from a GitHub read."""

import subprocess

import pytest

from omnimarket.events.pr_state import ModelPrCheckFact
from omnimarket.models.ci_red_triage import (
    EnumCiRedAction,
    EnumCiRedClass,
    ModelCiRedFacts,
    ModelCiRedTriageDecided,
    ModelCiRunFailedEvent,
    ci_run_failed_event_id,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.ci_red_claims import (
    ProjectionCiRedClaims,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_ci_red_triage import (
    EventCiRedFactsReader,
    HandlerCiRedTriage,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit


def fact(check: str, conclusion: str) -> ModelPrCheckFact:
    return ModelPrCheckFact(
        check=check,
        conclusion=conclusion,
        run_id=17000000001,
        workflow="CI",
        completed_at="2026-10-08T10:00:00Z",
    )


def red(
    *conclusions: tuple[str, str],
    base_red: tuple[str, ...] = (),
    base_read: bool = True,
    with_facts: bool = True,
) -> ModelCiRunFailedEvent:
    checks = tuple(sorted(check for check, _ in conclusions))
    return ModelCiRunFailedEvent(
        event_id=ci_run_failed_event_id("omniclaude", 2606, "head-2606", checks),
        repo="omniclaude",
        pr_number=2606,
        head_sha="head-2606",
        base="main",
        armed=True,
        queued=False,
        failing_checks=checks,
        ci_read_at="2026-10-08T10:00:00Z",
        observed_at="2026-10-08T10:00:00Z",
        source_digest="d" * 64,
        failing_runs=tuple(fact(c, k) for c, k in conclusions) if with_facts else (),
        base_red_checks=base_red,
        base_read=base_read,
    )


class UnavailableFactsReader:
    def __init__(self) -> None:
        self.reads = 0

    def read(self, event: ModelCiRunFailedEvent) -> ModelCiRedFacts:
        self.reads += 1
        return ModelCiRedFacts(event=event)


async def decide(event: ModelCiRunFailedEvent) -> tuple[ModelCiRedTriageDecided, int]:
    reader = UnavailableFactsReader()
    handler = HandlerCiRedTriage(
        facts_reader=EventCiRedFactsReader(fallback=reader),
        act=False,
        claims=ProjectionCiRedClaims(InmemoryDatabaseAdapter()),
    )
    output = await handler.handle(event)
    [decision] = [e for e in output.events if isinstance(e, ModelCiRedTriageDecided)]
    return decision, reader.reads


@pytest.mark.asyncio
async def test_runner_class_is_decided_from_the_event_conclusions() -> None:
    decision, reads = await decide(red(("unit", "timed_out")))
    assert decision.red_class == EnumCiRedClass.RUNNER
    assert decision.action == EnumCiRedAction.RERUN_FAILED
    assert "head conclusions" not in decision.evidence
    assert "base checks" not in decision.evidence
    assert "unread=annotations" in decision.evidence
    assert reads == 0


@pytest.mark.asyncio
async def test_dev_head_class_is_decided_from_the_event_base_reds() -> None:
    decision, reads = await decide(red(("unit", "failure"), base_red=("unit",)))
    assert decision.red_class == EnumCiRedClass.DEV_HEAD
    assert decision.owner_key == "dev:OmniNode-ai/omniclaude:main:unit"
    assert reads == 0


@pytest.mark.asyncio
async def test_a_red_the_base_does_not_carry_is_the_prs_own() -> None:
    decision, reads = await decide(red(("unit", "failure"), base_red=("lint",)))
    assert decision.red_class == EnumCiRedClass.PR_OWN
    assert reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        pytest.param(red(("unit", "failure"), with_facts=False), id="version-1-source"),
        pytest.param(red(("unit", "failure"), base_read=False), id="base-not-read"),
    ],
)
async def test_injected_reader_leaving_facts_unavailable_records_no_action(
    event: ModelCiRunFailedEvent,
) -> None:
    decision, reads = await decide(event)
    assert reads == 1
    assert "unread=head conclusions: unit; base checks" in decision.evidence
    assert "facts-unavailable" in decision.evidence


def test_failing_runs_may_only_name_failing_checks() -> None:
    with pytest.raises(ValueError, match="failing_runs must name failing_checks only"):
        ModelCiRunFailedEvent(
            **{
                **red(("unit", "failure")).model_dump(),
                "failing_runs": (fact("other", "failure"),),
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_read", "with_facts"), [(True, False), (False, True), (False, False)]
)
async def test_runtime_without_watcher_facts_records_unavailable_without_gh(
    monkeypatch: pytest.MonkeyPatch, base_read: bool, with_facts: bool
) -> None:
    """Probe the default handler, including armed peers, with the old-source shape."""
    from omnimarket.models.ci_red_triage import ModelCiRedPeer

    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"check_runs": [], "data": {}}')

    monkeypatch.setattr(subprocess, "run", run)
    event = red(("unit", "failure"), base_read=base_read, with_facts=with_facts)
    event = event.model_copy(
        update={
            "peers": (
                ModelCiRedPeer(
                    pr_number=2607,
                    head_sha="peer-head",
                    armed=True,
                    red_contexts=("unit",),
                ),
            )
        }
    )
    output = await HandlerCiRedTriage(
        act=True, claims=ProjectionCiRedClaims(InmemoryDatabaseAdapter())
    ).handle(event)
    assert calls == []
    [decision] = output.events
    assert isinstance(decision, ModelCiRedTriageDecided)
    assert decision.action == EnumCiRedAction.RECORD_ONLY
    assert decision.action_applied is False
    assert "facts-unavailable" in decision.evidence


@pytest.mark.asyncio
async def test_default_runtime_with_complete_watcher_facts_still_starts_pr_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        ModelPrLifecycleStartCommand,
    )

    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"check_runs": [], "data": {}}')

    monkeypatch.setattr(subprocess, "run", run)
    output = await HandlerCiRedTriage(
        act=True, claims=ProjectionCiRedClaims(InmemoryDatabaseAdapter())
    ).handle(red(("unit", "failure")))
    [start, decision] = output.events
    assert isinstance(start, ModelPrLifecycleStartCommand)
    assert start.pr_numbers == (2606,)
    assert isinstance(decision, ModelCiRedTriageDecided)
    assert decision.action == EnumCiRedAction.START_PR_FIX
    assert decision.action_applied is True
    assert "facts-unavailable" not in decision.evidence
    assert calls == []
