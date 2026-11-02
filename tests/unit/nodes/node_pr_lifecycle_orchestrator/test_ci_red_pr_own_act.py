"""The pr_own class acts on the bus: one owner fix start per PR head, and only when its flag is true.

Plan S6 flips ``ci_red_triage.act`` one class at a time, runner first, pr_own second.
A pr_own red (a red no other PR shares, not a runner red, not carried by the base head)
gets one scoped ``fix_only`` start of node_pr_lifecycle_orchestrator, whose FIXING phase
runs node_pr_lifecycle_fix_effect's delegated fix (plan S7). The decision records
``action_applied=true`` and the owner claim the landing controller reads (plan S5).
The flag is per class: with ``pr_own`` false the bus records the decision and starts
nothing, and the runner class keeps acting.
"""

from typing import Any

import pytest

from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.events.topics import CI_RED_TRIAGE_DECIDED_TOPIC_V1
from omnimarket.models.ci_red_triage import (
    EnumCiRedAction,
    EnumCiRedClass,
    ModelCiRedFacts,
    ModelCiRedTriageDecided,
    ModelCiRunFailedEvent,
    ci_red_owner_correlation_id,
    ci_red_owner_run_id,
    ci_run_failed_event_id,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.ci_red_claims import (
    ProjectionCiRedClaims,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_ci_red_triage import (
    HandlerCiRedTriage,
    ci_red_act_flags,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ModelPrLifecycleStartCommand,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)
from omnimarket.projection.pr_ledger_projection import PR_LEDGER_PROJECTION_TABLE
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

TESTS = "Tests / unit"
LINT = "Lint / ruff"
HEAD = "a" * 40
HEAD2 = "b" * 40
RUN_TESTS = 9101
SLUG = "OmniNode-ai/omnimarket"

# A recorded onex.evt.omnimarket.ci-run-failed.v1 value: PR 3700 is red on "Tests / unit"
# at HEAD while its two peers (3701, 3702) are armed and red on a different check, so
# the red is shared with no one.
RECORDED_PR_OWN_RED: dict[str, Any] = {
    "repo": "omnimarket",
    "pr_number": 3700,
    "head_sha": HEAD,
    "base": "dev",
    "armed": True,
    "queued": False,
    "failing_checks": [TESTS],
    "ci_read_at": "2026-11-02T15:00:00Z",
    "observed_at": "2026-11-02T15:00:00Z",
    "source_digest": "d" * 64,
    "peers": [
        {
            "pr_number": 3701,
            "head_sha": "c" * 40,
            "armed": True,
            "red_contexts": [LINT],
        },
        {
            "pr_number": 3702,
            "head_sha": "d" * 40,
            "armed": True,
            "red_contexts": [LINT],
        },
    ],
}

OWNER = f"{SLUG}#3700@{HEAD}:{TESTS}"


class Facts:
    """Head conclusions as GhCiRedFactsReader reads them; the base head is green."""

    def __init__(self, conclusions: dict[str, str]) -> None:
        self.conclusions = conclusions

    def read(self, ev: ModelCiRunFailedEvent) -> ModelCiRedFacts:
        return ModelCiRedFacts(
            event=ev,
            check_conclusions=self.conclusions,
            check_run_ids={TESTS: RUN_TESTS},
            base_read=True,
        )


PRODUCT_RED = Facts({TESTS: "failure", LINT: "failure"})


def recorded(**update: Any) -> dict[str, Any]:
    """The recorded value with its identity-derived event id (the model refuses any other)."""
    value = {**RECORDED_PR_OWN_RED, **update}
    value["event_id"] = ci_run_failed_event_id(
        value["repo"],
        value["pr_number"],
        value["head_sha"],
        sorted(value["failing_checks"]),
    )
    return value


def handler(
    database: InmemoryDatabaseAdapter,
    *,
    pr_own: bool,
    facts: Facts = PRODUCT_RED,
) -> HandlerCiRedTriage:
    """The handler as the runtime builds it, with the runner class acting and pr_own as given."""
    return HandlerCiRedTriage(
        facts_reader=facts,
        act={
            EnumCiRedClass.RUNNER: True,
            EnumCiRedClass.PR_OWN: pr_own,
            EnumCiRedClass.SHARED_CAUSE: False,
            EnumCiRedClass.DEV_HEAD: False,
        },
        claims=ProjectionCiRedClaims(database),
    )


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


def starts(outputs: list[Any]) -> list[ModelPrLifecycleStartCommand]:
    return [
        ev
        for output in outputs
        for ev in output.events
        if isinstance(ev, ModelPrLifecycleStartCommand)
    ]


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


# ---------------------------------------------------------------- AC1: the bus acts


@pytest.mark.asyncio
async def test_a_pr_own_red_among_three_members_has_exactly_one_owner_start() -> None:
    database = InmemoryDatabaseAdapter()
    output = await handler(database, pr_own=True).handle(recorded())
    project(database, [output])
    [start] = starts([output])
    [decided] = decisions([output])
    assert decided.red_class is EnumCiRedClass.PR_OWN
    assert decided.action is EnumCiRedAction.START_PR_FIX
    assert decided.action_applied is True
    assert decided.owner_key == OWNER
    assert decided.members == (3700,)
    # the one action: a scoped fix_only start for this PR, which runs the delegated fix
    assert start.repos == SLUG
    assert start.pr_numbers == (3700,)
    assert start.fix_only is True
    assert start.dry_run is False
    assert start.run_id == ci_red_owner_run_id(OWNER) == decided.orchestrator_run_id
    assert start.correlation_id == ci_red_owner_correlation_id(OWNER)
    assert reruns([output]) == []
    # the owner claim row the landing controller's deferral reads (plan S5)
    claims = database.query(
        PR_LEDGER_PROJECTION_TABLE,
        {"sweep_id": str(ci_red_owner_correlation_id(OWNER))},
    )
    assert [row["pr_number"] for row in claims] == [3700]
    assert str(claims[0]["evidence"]).startswith(f"claim=owner owner_key={OWNER} ")


@pytest.mark.asyncio
async def test_a_replayed_duplicate_yields_no_second_action() -> None:
    database = InmemoryDatabaseAdapter()
    live = handler(database, pr_own=True)
    outputs = [await live.handle(recorded())]
    project(database, outputs)
    outputs.append(await live.handle(recorded()))  # the same record redelivered
    # a restart forgets the process caches; the durable claim does not
    restarted = handler(database, pr_own=True)
    outputs.append(await restarted.handle(recorded()))
    # another detector pass: a wider failing set with the same deciding check
    outputs.append(
        await restarted.handle(recorded(failing_checks=[TESTS, "Zeta / last"]))
    )
    project(database, outputs[-1:])
    assert len(starts(outputs)) == 1
    assert [d.action_applied for d in decisions(outputs)] == [True]
    assert starts(outputs[1:]) == []


@pytest.mark.asyncio
async def test_a_new_head_is_a_cleared_cause_and_earns_its_own_one_start() -> None:
    database = InmemoryDatabaseAdapter()
    live = handler(database, pr_own=True)
    first = await live.handle(recorded())
    project(database, [first])
    second = await live.handle(recorded(head_sha=HEAD2))
    assert [s.pr_numbers for s in starts([first, second])] == [(3700,), (3700,)]
    assert {d.owner_key for d in decisions([first, second])} == {
        OWNER,
        f"{SLUG}#3700@{HEAD2}:{TESTS}",
    }


@pytest.mark.asyncio
async def test_a_wider_failing_set_with_the_same_deciding_check_acts_no_second_time() -> (
    None
):
    """The decision and the owner are keyed by PR, head and deciding check: a wider
    failing set that names the same first red was decided already, so it publishes nothing."""
    database = InmemoryDatabaseAdapter()
    live = handler(database, pr_own=True)
    first = await live.handle(recorded())
    project(database, [first])
    wider = await live.handle(recorded(failing_checks=[TESTS, "Zeta / last"]))
    assert len(starts([first, wider])) == 1
    assert wider.events == ()


@pytest.mark.asyncio
async def test_an_unarmed_pr_own_red_is_only_recorded() -> None:
    database = InmemoryDatabaseAdapter()
    output = await handler(database, pr_own=True).handle(recorded(armed=False))
    assert starts([output]) == []
    [decided] = decisions([output])
    assert decided.action is EnumCiRedAction.RECORD_ONLY
    assert decided.action_applied is False


@pytest.mark.asyncio
async def test_an_unreadable_claim_store_withholds_the_start() -> None:
    """With no claims DSN every start is withheld, so a restart cannot start twice."""
    output = await HandlerCiRedTriage(
        facts_reader=PRODUCT_RED,
        act={EnumCiRedClass.PR_OWN: True},
        claims=ProjectionCiRedClaims(None),
    ).handle(recorded())
    assert starts([output]) == []
    assert "start=withheld:claims-unread" in decisions([output])[0].evidence


# ------------------------------------------------- AC2: the flag is per class


@pytest.mark.asyncio
async def test_pr_own_false_starts_nothing_and_the_runner_class_still_reruns() -> None:
    """The rollback: ``pr_own`` false records the decision only. Positive control: in the
    same contract state a runner red still reruns, so the flag is per class."""
    database = InmemoryDatabaseAdapter()
    off = handler(database, pr_own=False)
    pr_own = await off.handle(recorded())
    [decided] = decisions([pr_own])
    assert decided.red_class is EnumCiRedClass.PR_OWN
    assert decided.action_applied is False
    assert "start=withheld:act=false" in decided.evidence
    assert starts([pr_own]) == []
    assert database.query(PR_LEDGER_PROJECTION_TABLE, {"evidence": None}) == []
    runner = await handler(
        database, pr_own=False, facts=Facts({TESTS: "timed_out"})
    ).handle(recorded(pr_number=3703, peers=[]))
    [request] = reruns([runner])
    assert request.run_ids == (RUN_TESTS,)
    assert decisions([runner])[0].action_applied is True


def test_the_contract_ships_pr_own_through_an_overlay_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract's pr_own flag is an overlay reference: bound false it records only,
    bound true it acts, and the runner flag moves with its own name alone."""
    monkeypatch.delenv("ONEX_CI_RED_TRIAGE_ACT_RUNNER", raising=False)
    monkeypatch.setenv("ONEX_CI_RED_TRIAGE_ACT_PR_OWN", "false")
    off = HandlerCiRedTriage(
        facts_reader=PRODUCT_RED,
        claims=ProjectionCiRedClaims(InmemoryDatabaseAdapter()),
    )
    assert off._act[EnumCiRedClass.PR_OWN] is False
    assert off._act[EnumCiRedClass.RUNNER] is True
    monkeypatch.setenv("ONEX_CI_RED_TRIAGE_ACT_PR_OWN", "true")
    on = HandlerCiRedTriage(
        facts_reader=PRODUCT_RED,
        claims=ProjectionCiRedClaims(InmemoryDatabaseAdapter()),
    )
    assert on._act[EnumCiRedClass.PR_OWN] is True
    assert ci_red_act_flags({"pr_own": "${env.ONEX_CI_RED_TRIAGE_ACT_PR_OWN:false}"})[
        EnumCiRedClass.PR_OWN
    ]


@pytest.mark.asyncio
async def test_a_contract_with_pr_own_false_starts_nothing_for_a_pr_own_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_CI_RED_TRIAGE_ACT_PR_OWN", "false")
    database = InmemoryDatabaseAdapter()
    contract_off = HandlerCiRedTriage(
        facts_reader=PRODUCT_RED, claims=ProjectionCiRedClaims(database)
    )
    output = await contract_off.handle(recorded())
    assert starts([output]) == []
    assert decisions([output])[0].action_applied is False
    # positive control: the same contract with the pr_own overlay bound true acts
    monkeypatch.setenv("ONEX_CI_RED_TRIAGE_ACT_PR_OWN", "true")
    contract_on = HandlerCiRedTriage(
        facts_reader=PRODUCT_RED, claims=ProjectionCiRedClaims(database)
    )
    assert len(starts([await contract_on.handle(recorded(pr_number=3704))])) == 1
