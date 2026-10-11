"""The runner class acts on the bus: one rerun per head, and only the runner class acts.

Plan S6 flips ``ci_red_triage.act`` one class at a time, runner first. A runner
red (the controller's ``runner_saturation`` or ``cancelled_producer``, one copy
of the rule in node_pr_lifecycle_triage_compute) gets its head's failed Actions
runs rerun once through node_pr_landing_github_effect's ``rerun_runs``, records
``action_applied=true`` and an owner claim the landing controller reads. Every
other class stays record-only.
"""

import json
import subprocess
from typing import Any

import pytest

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.events.pr_state import ModelPrCheckFact
from omnimarket.events.topics import (
    CI_RED_TRIAGE_DECIDED_TOPIC_V1,
    PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
)
from omnimarket.models.ci_red_triage import (
    EnumCiRedAction,
    EnumCiRedClass,
    ModelCiRedFacts,
    ModelCiRedTriageDecided,
    ModelCiRunFailedEvent,
    ci_red_owner_correlation_id,
    ci_run_failed_event_id,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.ci_red_claims import (
    ProjectionCiRedClaims,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_ci_red_triage import (
    EventCiRedFactsReader,
    GhCiRedFactsReader,
    HandlerCiRedTriage,
    ci_red_act_flags,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ModelPrLifecycleStartCommand,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_ci_red import (
    classify_ci_red,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_landing_red import (
    classify_red,
)
from omnimarket.projection.pr_ledger_projection import PR_LEDGER_PROJECTION_TABLE
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

TESTS = "Tests / unit"
LINT = "Lint / ruff"
SUMMARY = "CI Summary"
HEAD = "a" * 40
HEAD2 = "b" * 40
RUN_TESTS = 9101
RUN_LINT = 9102


def event(
    checks: tuple[str, ...] = (TESTS,),
    *,
    head: str = HEAD,
    pr: int = 3700,
    armed: bool = True,
) -> ModelCiRunFailedEvent:
    checks = tuple(sorted(checks))
    return ModelCiRunFailedEvent(
        event_id=ci_run_failed_event_id("omnimarket", pr, head, checks),
        repo="omnimarket",
        pr_number=pr,
        head_sha=head,
        base="dev",
        armed=armed,
        queued=False,
        failing_checks=checks,
        ci_read_at="2026-10-10T15:00:00Z",
        observed_at="2026-10-10T15:00:00Z",
        source_digest="d" * 64,
    )


class Facts:
    """Head conclusions and Actions run ids, as GhCiRedFactsReader reads them."""

    def __init__(
        self,
        conclusions: dict[str, str],
        run_ids: dict[str, int] | None = None,
    ) -> None:
        self.conclusions = conclusions
        self.run_ids = (
            {TESTS: RUN_TESTS, LINT: RUN_LINT, SUMMARY: RUN_TESTS}
            if run_ids is None
            else run_ids
        )

    def read(self, ev: ModelCiRunFailedEvent) -> ModelCiRedFacts:
        return ModelCiRedFacts(
            event=ev,
            check_conclusions=self.conclusions,
            check_run_ids=self.run_ids,
            base_read=True,
        )


TIMED_OUT = Facts({TESTS: "timed_out", LINT: "success"})


def project(database: InmemoryDatabaseAdapter, outputs: list[Any]) -> None:
    """node_pr_lifecycle_state_reducer over every published decision."""
    reducer = HandlerPrLifecycleStateReducer()
    for output in outputs:
        for decided in output.events:
            if isinstance(decided, ModelCiRedTriageDecided):
                reducer.handle_dict(
                    {
                        **decided.model_dump(mode="json"),
                        "_topic": CI_RED_TRIAGE_DECIDED_TOPIC_V1,
                        "_db": database,
                    }
                )


def reruns(outputs: list[Any]) -> list[ModelPrLandingGithubRequest]:
    return [
        ev
        for output in outputs
        for ev in output.events
        if isinstance(ev, ModelPrLandingGithubRequest)
    ]


def decisions(outputs: list[Any]) -> list[ModelCiRedTriageDecided]:
    return [
        ev
        for output in outputs
        for ev in output.events
        if isinstance(ev, ModelCiRedTriageDecided)
    ]


def contract_handler(
    facts: Facts, database: InmemoryDatabaseAdapter
) -> HandlerCiRedTriage:
    """The handler as the runtime builds it: act flags from the contract."""
    return HandlerCiRedTriage(
        facts_reader=facts, claims=ProjectionCiRedClaims(database)
    )


# ---------------------------------------------------------------- the contract flags


def test_act_flags_refuse_an_unknown_class_and_default_the_rest_off() -> None:
    with pytest.raises(ValueError, match="unknown red class 'flaky'"):
        ci_red_act_flags({"runner": True, "flaky": True})
    with pytest.raises(ValueError, match="must be true or false"):
        ci_red_act_flags({"runner": "yes"})
    with pytest.raises(ValueError, match="must map each red class"):
        ci_red_act_flags(True)
    assert ci_red_act_flags({"runner": True}) == {
        EnumCiRedClass.RUNNER: True,
        EnumCiRedClass.PR_OWN: False,
        EnumCiRedClass.SHARED_CAUSE: False,
        EnumCiRedClass.DEV_HEAD: False,
    }


def test_handler_refuses_a_contract_naming_an_unknown_class() -> None:
    misspelled: dict[Any, bool] = {"runner_class": True}
    with pytest.raises(ValueError, match="unknown red class"):
        HandlerCiRedTriage(
            facts_reader=TIMED_OUT,
            act=misspelled,
            claims=ProjectionCiRedClaims(InmemoryDatabaseAdapter()),
        )


def test_handler_publishes_the_rerun_on_the_github_effects_command_topic() -> None:
    topics = HandlerCiRedTriage.published_event_topics
    assert topics[ModelPrLandingGithubRequest] == PR_LANDING_GITHUB_REQUESTED_TOPIC_V1


# ------------------------------------------------------------------ the runner class


@pytest.mark.asyncio
async def test_a_runner_red_reruns_its_heads_failed_runs_once_and_claims_the_owner() -> (
    None
):
    database = InmemoryDatabaseAdapter()
    output = await contract_handler(TIMED_OUT, database).handle(event())
    project(database, [output])
    [request] = reruns([output])
    [decided] = decisions([output])
    owner = f"OmniNode-ai/omnimarket#3700@{HEAD}:rerun"
    assert request.operation is EnumPrLandingGithubOperation.RERUN_RUNS
    assert request.mode is EnumPrLandingGithubMode.ENFORCE
    assert request.repository == "OmniNode-ai/omnimarket"
    assert request.pr_number == 3700
    assert request.head_sha == HEAD
    assert request.run_ids == (RUN_TESTS,)
    assert request.correlation_id == ci_red_owner_correlation_id(owner)
    assert not any(isinstance(ev, ModelPrLifecycleStartCommand) for ev in output.events)
    assert decided.red_class is EnumCiRedClass.RUNNER
    assert decided.action is EnumCiRedAction.RERUN_FAILED
    assert decided.action_applied is True
    assert decided.owner_key == owner
    assert f"rerun_runs={RUN_TESTS}" in decided.evidence
    assert " owner_key=" + owner + " members=(" in decided.evidence
    # the owner claim row the landing controller's deferral reads (plan S5)
    claims = database.query(
        PR_LEDGER_PROJECTION_TABLE,
        {"sweep_id": str(ci_red_owner_correlation_id(owner))},
    )
    assert [row["pr_number"] for row in claims] == [3700]
    assert str(claims[0]["evidence"]).startswith(f"claim=owner owner_key={owner} ")


@pytest.mark.asyncio
async def test_a_cancelled_producer_is_the_runner_class_too() -> None:
    output = await contract_handler(
        Facts({TESTS: "cancelled", LINT: "cancelled"}), InmemoryDatabaseAdapter()
    ).handle(event((TESTS, LINT)))
    [request] = reruns([output])
    assert request.run_ids == (RUN_TESTS, RUN_LINT)
    assert decisions([output])[0].action_applied is True
    assert "cancelled_producer" in decisions([output])[0].evidence


@pytest.mark.asyncio
async def test_mistake_class_the_same_head_and_cause_is_never_rerun_twice() -> None:
    """Blocked PRs are not re-tried without a cleared cause: the same (head, cause) reruns once,
    across a replayed event, a second red at the head, and a runtime restart. A new head
    is a cleared cause and earns its own one rerun."""
    database = InmemoryDatabaseAdapter()
    handler = contract_handler(TIMED_OUT, database)
    outputs = [await handler.handle(event())]
    project(database, outputs)
    outputs.append(await handler.handle(event()))  # the same record redelivered
    second_red = Facts({TESTS: "timed_out", LINT: "cancelled"})
    handler._facts_reader = second_red
    outputs.append(
        await handler.handle(event((TESTS, LINT)))
    )  # a second red at the head
    project(database, outputs[-1:])
    for checks in ((TESTS,), (TESTS, LINT)):  # a restart forgets the process caches
        restarted = contract_handler(second_red, database)
        outputs.append(await restarted.handle(event(checks)))
        project(database, outputs[-1:])
    assert len(reruns(outputs)) == 1
    actions = [d.action for d in decisions(outputs)]
    assert actions == [EnumCiRedAction.RERUN_FAILED, EnumCiRedAction.JOINED_OWNER]
    assert [d.action_applied for d in decisions(outputs)] == [True, False]
    new_head = await contract_handler(second_red, database).handle(
        event((TESTS, LINT), head=HEAD2)
    )
    assert [r.head_sha for r in reruns([new_head])] == [HEAD2]


@pytest.mark.asyncio
async def test_a_reviewer_pool_red_stays_with_the_controller() -> None:
    hostile = "Hostile Reviewer (adversarial gate)"
    output = await contract_handler(
        Facts({hostile: "failure"}, {hostile: 77}), InmemoryDatabaseAdapter()
    ).handle(event((hostile,)))
    [decided] = decisions([output])
    assert decided.red_class is EnumCiRedClass.RUNNER
    assert reruns([output]) == []
    assert decided.action_applied is False
    assert "start=withheld:reviewer_pool" in decided.evidence


class NoGitHubRead:
    def read(self, ev: ModelCiRunFailedEvent) -> ModelCiRedFacts:
        raise AssertionError("the event carries its facts; no GitHub read")


@pytest.mark.asyncio
async def test_an_event_that_carries_its_check_facts_reruns_from_their_actions_runs() -> (
    None
):
    """A version 2 event states each failing check's Actions run, so the rerun needs no GitHub read."""
    checks = (LINT, TESTS)
    carried = event(checks).model_copy(
        update={
            "failing_runs": tuple(
                ModelPrCheckFact(
                    check=check,
                    conclusion="timed_out",
                    run_id=run_id,
                    workflow="CI",
                    completed_at="2026-10-10T15:00:00Z",
                )
                for check, run_id in ((LINT, RUN_LINT), (TESTS, RUN_TESTS))
            ),
            "base_read": True,
        }
    )
    handler = HandlerCiRedTriage(
        facts_reader=EventCiRedFactsReader(fallback=NoGitHubRead()),
        claims=ProjectionCiRedClaims(InmemoryDatabaseAdapter()),
    )
    output = await handler.handle(carried)
    [request] = reruns([output])
    [decided] = decisions([output])
    assert request.run_ids == (RUN_TESTS, RUN_LINT)
    assert decided.action_applied is True
    assert decided.red_class is EnumCiRedClass.RUNNER


@pytest.mark.asyncio
async def test_a_runner_red_with_no_actions_run_is_withheld_and_claims_nothing() -> (
    None
):
    database = InmemoryDatabaseAdapter()
    output = await contract_handler(
        Facts({TESTS: "timed_out"}, run_ids={}), database
    ).handle(event())
    project(database, [output])
    [decided] = decisions([output])
    assert reruns([output]) == []
    assert decided.action_applied is False
    assert "start=withheld:no-run-ids" in decided.evidence
    assert decided.claimed_members() == ()


@pytest.mark.asyncio
async def test_an_unarmed_runner_red_is_only_recorded() -> None:
    output = await contract_handler(TIMED_OUT, InmemoryDatabaseAdapter()).handle(
        event(armed=False)
    )
    assert reruns([output]) == []
    assert decisions([output])[0].action is EnumCiRedAction.RECORD_ONLY


# -------------------------------------------------------- every other class records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("facts", "red_class"),
    [
        (Facts({TESTS: "failure"}), EnumCiRedClass.PR_OWN),
        (Facts({TESTS: "timed_out"}), EnumCiRedClass.DEV_HEAD),
    ],
)
async def test_every_other_class_stays_record_only(
    facts: Facts, red_class: EnumCiRedClass
) -> None:
    class BaseRed(Facts):
        def read(self, ev: ModelCiRunFailedEvent) -> ModelCiRedFacts:
            base = (TESTS,) if red_class is EnumCiRedClass.DEV_HEAD else ()
            return super().read(ev).model_copy(update={"base_red_checks": base})

    database = InmemoryDatabaseAdapter()
    output = await contract_handler(BaseRed(facts.conclusions), database).handle(
        event()
    )
    project(database, [output])
    [decided] = output.events
    assert isinstance(decided, ModelCiRedTriageDecided)
    assert decided.red_class is red_class
    assert decided.action_applied is False
    assert "start=withheld:act=false" in decided.evidence
    assert database.query(PR_LEDGER_PROJECTION_TABLE, {"evidence": None}) == []


# ----------------------------------------------------- one copy of the red rules (S3)


@pytest.mark.parametrize(
    "conclusions",
    [
        {TESTS: "timed_out"},
        {TESTS: "cancelled"},
        {TESTS: "timed_out", LINT: "cancelled"},
        {TESTS: "timed_out", LINT: "failure"},
        {TESTS: "failure"},
        {TESTS: "cancelled", SUMMARY: "failure"},
        {SUMMARY: "failure"},
        {},
    ],
)
def test_the_runner_class_is_the_controllers_classify_red(
    conclusions: dict[str, str],
) -> None:
    checks = tuple(sorted(conclusions)) or (TESTS,)
    facts = ModelCiRedFacts(event=event(checks), check_conclusions=conclusions)
    landing = classify_red(
        checks,
        [(n, "completed", c) for n, c in conclusions.items()],
        companion_merged=False,
    )
    verdict = classify_ci_red(facts)
    runner = landing in {"runner_saturation", "cancelled_producer"}
    assert (verdict.red_class is EnumCiRedClass.RUNNER) is runner
    assert verdict.landing_red_class == (landing if runner else None)


# ------------------------------------------------------------- the facts reader


def test_gh_reader_reads_each_checks_newest_actions_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {}}))
        base = "https://github.com/OmniNode-ai/omnimarket"
        runs = [
            {
                "id": 1,
                "name": TESTS,
                "started_at": "2026-10-10T10:00:00Z",
                "conclusion": "failure",
                "details_url": f"{base}/actions/runs/500/job/1",
            },
            {
                "id": 2,
                "name": TESTS,
                "started_at": "2026-10-10T11:00:00Z",
                "conclusion": "timed_out",
                "details_url": f"{base}/actions/runs/{RUN_TESTS}/job/2",
            },
            {
                "id": 3,
                "name": "external",
                "started_at": "2026-10-10T11:00:00Z",
                "conclusion": "timed_out",
                "details_url": "https://ci.example.invalid/build/3",
            },
        ]
        return subprocess.CompletedProcess(args, 0, json.dumps({"check_runs": runs}))

    monkeypatch.setattr(subprocess, "run", run)
    facts = GhCiRedFactsReader().read(event())
    assert facts.check_conclusions == {TESTS: "timed_out", "external": "timed_out"}
    assert facts.check_run_ids == {TESTS: RUN_TESTS}


# ------------------------------------------------- the runtime path to the effect


@pytest.mark.asyncio
async def test_runtime_routes_the_rerun_to_the_github_effects_rerun_failed_jobs() -> (
    None
):
    """The handler output as the runtime normalizes and publishes it: the rerun goes on the
    topic node_pr_landing_github_effect subscribes, which plans one rerun-failed-jobs per run."""
    from pathlib import Path
    from types import SimpleNamespace
    from uuid import NAMESPACE_URL, uuid5

    import yaml
    from omnibase_core.enums.enum_node_kind import EnumNodeKind
    from omnibase_core.runtime.runtime_fanout_resolver import resolve_published_topic
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _normalize_handler_result,
        _topics_for_handler_entry,
    )

    from omnimarket.nodes.node_pr_landing_github_effect.handlers.handler_pr_landing_github import (
        HandlerPrLandingGithubEffect,
    )

    nodes = Path("src/omnimarket/nodes")
    orchestrator = yaml.safe_load(
        (nodes / "node_pr_lifecycle_orchestrator" / "contract.yaml").read_text()
    )
    output = await contract_handler(
        Facts({TESTS: "timed_out", LINT: "cancelled"}), InmemoryDatabaseAdapter()
    ).handle(event((TESTS, LINT)).model_dump(mode="json"))
    envelope = SimpleNamespace(payload={}, correlation_id=uuid5(NAMESPACE_URL, "s6"))
    normalized = _normalize_handler_result(
        output, envelope, None, EnumNodeKind.ORCHESTRATOR
    )
    assert normalized is not None
    published = {r["event_type"]: r["topic"] for r in orchestrator["published_events"]}
    topics = [resolve_published_topic(published, ev) for ev in normalized.output_events]
    assert topics == [
        PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
        CI_RED_TRIAGE_DECIDED_TOPIC_V1,
    ]
    assert set(topics) <= set(orchestrator["event_bus"]["publish_topics"])

    effect_path = nodes / "node_pr_landing_github_effect" / "contract.yaml"
    manifest = discover_contracts_from_paths([effect_path])
    assert not manifest.errors
    effect = manifest.contracts[0]
    assert effect.handler_routing is not None
    [entry] = effect.handler_routing.handlers
    assert _topics_for_handler_entry(effect, entry) == (
        PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
    )
    wire = ModelPrLandingGithubRequest.model_validate(
        normalized.output_events[0].model_dump(mode="json")
    )
    planned = await HandlerPrLandingGithubEffect().handle(
        wire.model_copy(update={"mode": EnumPrLandingGithubMode.DRY_RUN})
    )
    base = "/repos/OmniNode-ai/omnimarket/actions/runs"
    assert [(r.method, r.path) for r in planned.requests] == [
        ("POST", f"{base}/{RUN_TESTS}/rerun-failed-jobs"),
        ("POST", f"{base}/{RUN_LINT}/rerun-failed-jobs"),
    ]


# ------------------------------------------- per-lane act overlay (OMN-20867)

ACT_ENV = {
    EnumCiRedClass.RUNNER: "ONEX_CI_RED_TRIAGE_ACT_RUNNER",
    EnumCiRedClass.PR_OWN: "ONEX_CI_RED_TRIAGE_ACT_PR_OWN",
    EnumCiRedClass.SHARED_CAUSE: "ONEX_CI_RED_TRIAGE_ACT_SHARED_CAUSE",
    EnumCiRedClass.DEV_HEAD: "ONEX_CI_RED_TRIAGE_ACT_DEV_HEAD",
}


def _unbind(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ACT_ENV.values():
        monkeypatch.delenv(name, raising=False)


def test_overlay_unset_the_contract_acts_on_the_runner_class_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unbind(monkeypatch)
    handler = contract_handler(TIMED_OUT, InmemoryDatabaseAdapter())
    assert handler._act == {
        EnumCiRedClass.RUNNER: True,
        EnumCiRedClass.PR_OWN: False,
        EnumCiRedClass.SHARED_CAUSE: False,
        EnumCiRedClass.DEV_HEAD: False,
    }


@pytest.mark.asyncio
async def test_overlay_non_acting_records_a_runner_red_and_reruns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unbind(monkeypatch)
    for name in ACT_ENV.values():
        monkeypatch.setenv(name, "false")
    handler = contract_handler(TIMED_OUT, InmemoryDatabaseAdapter())
    assert not any(handler._act.values())
    output = await handler.handle(event())
    assert reruns([output]) == []
    assert not any(isinstance(ev, ModelPrLifecycleStartCommand) for ev in output.events)
    [decided] = decisions([output])
    assert decided.red_class is EnumCiRedClass.RUNNER
    assert decided.action_applied is False
    assert "start=withheld:act=false" in decided.evidence


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.asyncio
async def test_overlay_acting_reruns_a_runner_red(
    monkeypatch: pytest.MonkeyPatch, bound: bool
) -> None:
    """The positive control: unset, or bound to true, the runner red is rerun."""
    _unbind(monkeypatch)
    if bound:
        monkeypatch.setenv(ACT_ENV[EnumCiRedClass.RUNNER], "true")
    output = await contract_handler(TIMED_OUT, InmemoryDatabaseAdapter()).handle(
        event()
    )
    [request] = reruns([output])
    assert request.operation is EnumPrLandingGithubOperation.RERUN_RUNS
    assert decisions([output])[0].action_applied is True


@pytest.mark.parametrize("value", ["yes", "True", "0", ""])
def test_overlay_a_malformed_value_fails_at_contract_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _unbind(monkeypatch)
    monkeypatch.setenv(ACT_ENV[EnumCiRedClass.RUNNER], value)
    with pytest.raises(
        ValueError, match=r"ci_red_triage\.act\.runner must be true or false"
    ):
        contract_handler(TIMED_OUT, InmemoryDatabaseAdapter())


def test_overlay_refs_expand_and_an_unbound_ref_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_TEST_UNBOUND_ACT", raising=False)
    monkeypatch.setenv("ONEX_TEST_BOUND_ACT", "true")
    assert ci_red_act_flags(
        {
            "runner": "${env.ONEX_TEST_BOUND_ACT}",
            "pr_own": "${env.ONEX_TEST_UNBOUND_ACT:false}",
        }
    ) == {
        EnumCiRedClass.RUNNER: True,
        EnumCiRedClass.PR_OWN: False,
        EnumCiRedClass.SHARED_CAUSE: False,
        EnumCiRedClass.DEV_HEAD: False,
    }
    with pytest.raises(ValueError, match="must be true or false"):
        ci_red_act_flags({"runner": "${env.ONEX_TEST_UNBOUND_ACT}"})
