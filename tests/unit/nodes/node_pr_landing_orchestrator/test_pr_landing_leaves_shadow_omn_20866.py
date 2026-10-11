# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing orchestrator leaves shadow mode for its canary (OMN-20866).

AC1 (``-k config``): the handler builds its config from the contract's
``landing_config`` block, and a handler that ignores the declared mode fails
here. AC2 (``-k enforce``): for the canary repository a green PR, replayed from
recorded check runs through the real classifier and the real arm gate, reaches
READY then ARMED with an enforce arm; reruns, update-branch and disarm stay
dry_run; a declined companion in a repository that still needs one goes to
NEEDS_AGENT. The classifier tests replay recorded check runs of omnimarket
heads through ``classify_head_checks``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from omnimarket.events.pr_arm_gate import EnumArmActionMode
from omnimarket.events.pr_landing_companion import ModelPrLandingCompanionOutcome
from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubCheckRunFact,
    ModelGithubPrStateFact,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingAgentReason,
    EnumPrLandingState,
    ModelPrLandingAgentNeeded,
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingCompanionOutcomeIngress,
    ModelPrLandingGithubCompletedIngress,
    ModelPrLandingObservedPrompt,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingWorkflowRow,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.contract_config import (
    PrLandingContractConfigError,
    config_from_block,
    load_contract_config,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.head_checks import (
    TriageHeadCheckClassifier,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.ports import (
    ProtocolPrLandingHeadCheckClassifier,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_verdict import (
    ModelHeadCheckVerdict,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    NODE_ID,
    T0,
    FixedClassifier,
    answer,
    only_request,
    prompt,
)

pytestmark = pytest.mark.unit

CANARY = "OmniNode-ai/omnimarket"
OTHER = "OmniNode-ai/omniclaude"
_REPLAY = Path(__file__).resolve().parents[3] / "fixtures/pr_landing/head_checks_replay"


def _recorded(pr: int) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (_REPLAY / f"omnimarket_{pr}.json").read_text("utf-8")
    )
    return data


def _contract_handler(
    *,
    classifier: ProtocolPrLandingHeadCheckClassifier | None = None,
    store: InMemoryPrLandingRowStore | None = None,
    config: PrLandingOrchestratorConfig | None = None,
) -> HandlerPrLandingOrchestrator:
    """The handler as the runtime builds it: real reducer, gate and classifier.

    No ``config`` means the handler's own default, the contract's block.
    """
    return HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=HandlerPrArmGate(),
        classifier=classifier,
        config=config,
        store=store if store is not None else InMemoryPrLandingRowStore(),
    )


def _observed(
    repo: str, pr: int, head: str, *, state: str = "open", minutes: float = 0
) -> ModelPrLandingObservedPrompt:
    """A PR watcher payload as it reaches the bus, envelope fields included."""
    at = (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ModelPrLandingObservedPrompt.model_validate(
        {
            "repo": repo.split("/", 1)[1],
            "pr_number": pr,
            "state": state,
            "head_sha": head,
            "observed_at": at,
            "base": "dev",
            "armed": True,
            "ci_verdict": "GREEN",
            "red_contexts": [],
            "actor": "claude",
            "hook_fired_at": at,
            "schema_version": "1.0.0",
        }
    )


def _pr_state(
    pr: int, head: str, *, mergeable_state: str | None = "clean"
) -> ModelGithubPrStateFact:
    return ModelGithubPrStateFact.model_validate(
        {
            "pr_number": pr,
            "head_sha": head,
            "base_ref": "dev",
            "state": "open",
            "merged": False,
            "draft": False,
            "title": f"feat(OMN-20866): canary pr {pr}",
            "labels": (),
            "auto_merge_armed": True,
            "pr_node_id": NODE_ID,
            "mergeable_state": mergeable_state,
        }
    )


def _head_checks_answer(
    request: ModelPrLandingGithubRequest, recorded: dict[str, Any]
) -> ModelPrLandingGithubCompletedIngress:
    base = answer(
        request,
        check_runs=ModelGithubCheckRunFact.from_check_runs_body(
            recorded["check_runs_body"]
        ),
    )
    return base.model_copy(
        update={"required_contexts": tuple(recorded["required_contexts"])}
    )


def _transitions(emitted: list[BaseModel]) -> list[ModelPrLandingTransitioned]:
    return [e for e in emitted if isinstance(e, ModelPrLandingTransitioned)]


def _states(emitted: list[BaseModel]) -> list[EnumPrLandingState]:
    return [t.to_state for t in _transitions(emitted)]


async def _canary_to_head_check_read(
    handler: HandlerPrLandingOrchestrator,
    recorded: dict[str, Any],
    *,
    mergeable_state: str | None = "clean",
) -> ModelPrLandingGithubRequest:
    """Watcher prompt, PR read answered open and armed; return the head-check read."""
    pr, head = recorded["pr_number"], recorded["head_sha"]
    read = only_request(await handler.handle(_observed(CANARY, pr, head)))
    assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    emitted = await handler.handle(
        answer(read, pr_state=_pr_state(pr, head, mergeable_state=mergeable_state))
    )
    head_read = only_request(emitted)
    assert head_read.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
    return head_read


# ------------------------------------------------------------------- AC1 config


def test_config_the_contract_declares_the_canary() -> None:
    config = load_contract_config()
    assert config.github_mode is EnumPrLandingGithubMode.DRY_RUN
    assert config.github_mode_for(CANARY) is EnumPrLandingGithubMode.ENFORCE
    assert config.github_mode_for(OTHER) is EnumPrLandingGithubMode.DRY_RUN
    assert not config.companion_required(CANARY)
    assert config.companion_required(OTHER)
    assert config.dry_run_operations == {
        EnumPrLandingGithubOperation.RERUN_RUNS,
        EnumPrLandingGithubOperation.UPDATE_BRANCH,
        EnumPrLandingGithubOperation.DISARM,
    }
    # The other M4 repositories' entries: test_pr_landing_m4_repositories_omn_20866.
    assert CANARY not in config.queue_repos
    assert CANARY in config.observed_prompt_repos
    assert CANARY in config.review_threads_not_required_repos
    assert config.arm_policy.action_mode is EnumArmActionMode.ENFORCE
    assert config.arm_policy.kill_switch is False


async def test_config_the_handler_sends_the_contracts_mode_for_each_repository() -> (
    None
):
    """A handler on the code default (dry_run everywhere) fails both halves."""
    handler = _contract_handler(classifier=FixedClassifier(EnumHeadCheckVerdict.GREEN))
    recorded = _recorded(3639)
    head_read = await _canary_to_head_check_read(handler, recorded)
    arm = only_request(await handler.handle(answer(head_read)))
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert arm.mode is EnumPrLandingGithubMode.ENFORCE

    # Any other repository keeps dry_run, its companion command included.
    other = _contract_handler(classifier=FixedClassifier())
    read = only_request(await other.handle(prompt(repo=OTHER)))
    emitted = await other.handle(
        answer(read, pr_state=_pr_state(4242, "1" * 40).model_copy())
    )
    (command,) = [e for e in emitted if isinstance(e, ModelPrLifecycleFixCommand)]
    assert command.repo == OTHER
    assert command.dry_run is True


async def test_config_a_dry_run_declared_for_the_canary_is_obeyed() -> None:
    block = {
        "github_mode": "dry_run",
        "github_mode_by_repository": {CANARY: "dry_run"},
        "companion_exempt_repositories": [CANARY],
        "review_threads_not_required_repositories": [CANARY],
        "observed_prompt_repositories": [CANARY],
        "arm_policy": {"action_mode": "enforce", "kill_switch": False},
    }
    handler = _contract_handler(
        classifier=FixedClassifier(), config=config_from_block(block)
    )
    head_read = await _canary_to_head_check_read(handler, _recorded(3639))
    arm = only_request(await handler.handle(answer(head_read)))
    assert arm.mode is EnumPrLandingGithubMode.DRY_RUN


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        (None, "no landing_config"),
        ({"github_mode": "dry_run", "surprise": 1}, "unknown keys"),
        ({}, "github_mode is required"),
        ({"github_mode": "loud"}, "not one of dry_run or enforce"),
        (
            {"github_mode": "dry_run", "github_mode_by_repository": {CANARY: "x"}},
            "not one of dry_run or enforce",
        ),
        ({"github_mode": "dry_run", "dry_run_operations": ["merge"]}, "dry_run_op"),
        (
            {"github_mode": "dry_run", "dry_run_operations": ["read_pr_state"]},
            "reads are always sent",
        ),
        (
            {"github_mode": "dry_run", "companion_exempt_repositories": ["omnimarket"]},
            "owner/name",
        ),
    ],
)
def test_config_a_malformed_block_is_refused(block: object, fragment: str) -> None:
    with pytest.raises(PrLandingContractConfigError, match=fragment):
        config_from_block(block)


# ------------------------------------------------------------------ AC2 enforce


async def test_enforce_a_green_canary_pr_reaches_ready_then_armed_on_replay() -> None:
    """Recorded check runs of omnimarket#3639, the real classifier and arm gate."""
    store = InMemoryPrLandingRowStore()
    handler = _contract_handler(store=store)
    recorded = _recorded(3639)
    head_read = await _canary_to_head_check_read(handler, recorded)
    assert head_read.base_ref == "dev"
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    assert _states(emitted) == [EnumPrLandingState.READY]
    arm = only_request(emitted)
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert arm.mode is EnumPrLandingGithubMode.ENFORCE
    assert arm.head_sha == recorded["head_sha"]
    emitted = await handler.handle(answer(arm))
    assert _states(emitted) == [EnumPrLandingState.ARMED]


async def test_enforce_a_red_canary_pr_goes_to_an_agent_with_nothing_armed() -> None:
    """Recorded check runs of omnimarket#3643: three required contexts failed."""
    handler = _contract_handler()
    recorded = _recorded(3643)
    head_read = await _canary_to_head_check_read(handler, recorded)
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    assert _states(emitted) == [EnumPrLandingState.NEEDS_AGENT]
    agents = [e for e in emitted if isinstance(e, ModelPrLandingAgentNeeded)]
    assert [a.reason for a in agents] == [EnumPrLandingAgentReason.REAL_RED]
    assert [r for r in emitted if isinstance(r, ModelPrLandingGithubRequest)] == []


async def test_enforce_a_green_head_github_still_reports_blocked_stays_pending() -> (
    None
):
    store = InMemoryPrLandingRowStore()
    handler = _contract_handler(store=store)
    recorded = _recorded(3639)
    head_read = await _canary_to_head_check_read(
        handler, recorded, mergeable_state="blocked"
    )
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    assert _states(emitted) == [EnumPrLandingState.CHECKS_PENDING]
    assert [r for r in emitted if isinstance(r, ModelPrLandingGithubRequest)] == []


async def test_enforce_reruns_stay_dry_run_for_the_canary() -> None:
    handler = _contract_handler(classifier=_RerunClassifier("CI Summary"))
    recorded = _recorded(3643)
    head_read = await _canary_to_head_check_read(handler, recorded)
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    rerun = only_request(emitted)
    assert rerun.operation is EnumPrLandingGithubOperation.RERUN_RUNS
    assert rerun.mode is EnumPrLandingGithubMode.DRY_RUN


class _RerunClassifier:
    """A timed-out verdict naming one recorded check, for the rerun-mode test."""

    def __init__(self, check: str) -> None:
        self.check = check

    async def classify(
        self,
        completed: ModelPrLandingGithubCompleted,
        row: ModelPrLandingWorkflowRow,
    ) -> ModelHeadCheckVerdict:
        assert completed.head_sha is not None
        return ModelHeadCheckVerdict(
            repository=completed.repository,
            pr_number=completed.pr_number,
            head_sha=completed.head_sha,
            verdict=EnumHeadCheckVerdict.TIMED_OUT,
            rerun_checks=(self.check,),
        )


@pytest.mark.parametrize("mode", ["dry_run", "enforce"])
async def test_enforce_a_declined_companion_outside_the_exempt_set_needs_an_agent(
    mode: str,
) -> None:
    """A repository that still needs a companion is paged on a decline, either mode."""
    config = config_from_block(
        {"github_mode": "dry_run", "github_mode_by_repository": {OTHER: mode}}
    )
    store = InMemoryPrLandingRowStore()
    handler = _contract_handler(
        classifier=FixedClassifier(), config=config, store=store
    )
    read = only_request(await handler.handle(prompt(repo=OTHER)))
    emitted = await handler.handle(answer(read, pr_state=_pr_state(4242, "1" * 40)))
    assert _states(emitted)[-1] is EnumPrLandingState.COMPANION_PENDING
    (command,) = [e for e in emitted if isinstance(e, ModelPrLifecycleFixCommand)]
    assert command.dry_run is (mode == "dry_run")
    outcome = ModelPrLandingCompanionOutcomeIngress.model_validate(
        ModelPrLandingCompanionOutcome(
            kind="DECLINED",
            op="derive",
            repository=OTHER,
            pr_number=4242,
            head_sha="1" * 40,
            correlation_id=command.correlation_id,
            decline_code="UNCLASSIFIED",
            decline_reason="OCC companion NOT verified",
        ).model_dump()
    )
    emitted = await handler.handle(outcome)
    assert _states(emitted) == [EnumPrLandingState.NEEDS_AGENT]
    agents = [e for e in emitted if isinstance(e, ModelPrLandingAgentNeeded)]
    assert [a.reason for a in agents] == [EnumPrLandingAgentReason.COMPANION_DECLINED]


# --------------------------------------------------------- observed prompts (I3)


async def test_enforce_an_observation_outside_the_canary_is_dropped() -> None:
    handler = _contract_handler()
    assert await handler.handle(_observed(OTHER, 7, "1" * 40)) == []


async def test_enforce_first_sight_of_a_merged_pr_is_dropped() -> None:
    handler = _contract_handler()
    assert await handler.handle(_observed(CANARY, 7, "1" * 40, state="merged")) == []


async def test_enforce_an_observation_while_a_read_is_in_flight_sends_nothing() -> None:
    handler = _contract_handler()
    only_request(await handler.handle(_observed(CANARY, 7, "1" * 40)))
    assert await handler.handle(_observed(CANARY, 7, "1" * 40, minutes=1)) == []


# ------------------------------------------------------------ classifier replay


@pytest.mark.parametrize(
    ("pr", "required", "verdict"),
    [
        # Every required context passed; the one red is a post-merge check that
        # no ruleset requires.
        (3639, True, EnumHeadCheckVerdict.GREEN),
        # Without the required contexts every check blocks: stricter, never looser.
        (3639, False, EnumHeadCheckVerdict.PRODUCT_FAILED),
        (3643, True, EnumHeadCheckVerdict.PRODUCT_FAILED),
    ],
)
async def test_the_classifier_replays_recorded_check_runs(
    pr: int, required: bool, verdict: EnumHeadCheckVerdict
) -> None:
    recorded = _recorded(pr)
    request = ModelPrLandingGithubRequest(
        correlation_id=uuid4(),
        operation=EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        mode=EnumPrLandingGithubMode.ENFORCE,
        repository=CANARY,
        pr_number=pr,
        head_sha=recorded["head_sha"],
        base_ref="dev",
    )
    completed = _head_checks_answer(request, recorded)
    if not required:
        completed = completed.model_copy(update={"required_contexts": None})
    row = _row_for(pr)
    got = await TriageHeadCheckClassifier().classify(completed, row)
    assert got.verdict is verdict
    assert got.head_sha == recorded["head_sha"]


async def test_a_required_context_with_no_check_run_yet_is_pending() -> None:
    recorded = _recorded(3639)
    request = ModelPrLandingGithubRequest(
        correlation_id=uuid4(),
        operation=EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        mode=EnumPrLandingGithubMode.ENFORCE,
        repository=CANARY,
        pr_number=3639,
        head_sha=recorded["head_sha"],
    )
    completed = _head_checks_answer(request, recorded)
    completed = completed.model_copy(
        update={
            "required_contexts": (
                *recorded["required_contexts"],
                "a required gate not posted yet",
            )
        }
    )
    got = await TriageHeadCheckClassifier().classify(completed, _row_for(3639))
    assert got.verdict is EnumHeadCheckVerdict.PENDING


def _row_for(pr: int) -> ModelPrLandingWorkflowRow:
    return ModelPrLandingWorkflowRow(
        repository=CANARY,
        pr_number=pr,
        landing_key=f"{CANARY}#{pr}",
        updated_at=T0,
        base_ref="dev",
        merge_state_status="clean",
    )


# ------------------------------------------- per-lane act overlay (OMN-20867)

MODE_ENV = "ONEX_PR_LANDING_GITHUB_MODE"
ARM_ENV = "ONEX_PR_LANDING_ARM_ACTION_MODE"
_READ_OPERATIONS = {
    EnumPrLandingGithubOperation.READ_PR_STATE,
    EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
}
# The block as it was declared before the act values took a lane overlay.
_SHIPPED_BLOCK: dict[str, object] = {
    "github_mode": "dry_run",
    "github_mode_by_repository": {CANARY: "enforce"},
    "dry_run_operations": ["rerun_runs", "update_branch", "disarm"],
    "companion_exempt_repositories": [CANARY, "OmniNode-ai/omnibase_infra"],
    "queue_repositories": ["OmniNode-ai/omnibase_infra"],
    "review_threads_not_required_repositories": [
        CANARY,
        "OmniNode-ai/omnibase_infra",
        "OmniNode-ai/omnibase_core",
        "OmniNode-ai/omniclaude",
        "OmniNode-ai/omnidash",
    ],
    "observed_prompt_repositories": [CANARY, "OmniNode-ai/omnibase_infra"],
    "arm_policy": {"action_mode": "enforce", "kill_switch": False},
}


async def _ready_canary_requests(
    handler: HandlerPrLandingOrchestrator,
) -> list[ModelPrLandingGithubRequest]:
    """Every GitHub request one green canary PR draws, through READY."""
    recorded = _recorded(3639)
    pr, head = recorded["pr_number"], recorded["head_sha"]
    sent: list[ModelPrLandingGithubRequest] = []
    emitted = await handler.handle(_observed(CANARY, pr, head))
    sent += [e for e in emitted if isinstance(e, ModelPrLandingGithubRequest)]
    emitted = await handler.handle(answer(sent[-1], pr_state=_pr_state(pr, head)))
    sent += [e for e in emitted if isinstance(e, ModelPrLandingGithubRequest)]
    assert sent[-1].operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
    emitted = await handler.handle(_head_checks_answer(sent[-1], recorded))
    assert _states(emitted) == [EnumPrLandingState.READY]
    sent += [e for e in emitted if isinstance(e, ModelPrLandingGithubRequest)]
    return sent


def test_overlay_unset_the_contract_declares_todays_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MODE_ENV, raising=False)
    monkeypatch.delenv(ARM_ENV, raising=False)
    assert load_contract_config() == config_from_block(_SHIPPED_BLOCK)


async def test_overlay_non_acting_sends_only_reads_for_a_ready_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MODE_ENV, "dry_run")
    monkeypatch.setenv(ARM_ENV, "report_only")
    config = load_contract_config()
    assert config.github_mode_for(CANARY) is EnumPrLandingGithubMode.DRY_RUN
    assert config.arm_policy.action_mode is EnumArmActionMode.REPORT_ONLY
    sent = await _ready_canary_requests(_contract_handler())
    assert [r.operation for r in sent] == [
        EnumPrLandingGithubOperation.READ_PR_STATE,
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
    ]
    assert {r.operation for r in sent} <= _READ_OPERATIONS


@pytest.mark.parametrize("bound", [False, True])
async def test_overlay_acting_arms_a_ready_pr(
    monkeypatch: pytest.MonkeyPatch, bound: bool
) -> None:
    """The positive control: unset, or bound to acting, the canary is armed."""
    if bound:
        monkeypatch.setenv(MODE_ENV, "enforce")
        monkeypatch.setenv(ARM_ENV, "enforce")
    else:
        monkeypatch.delenv(MODE_ENV, raising=False)
        monkeypatch.delenv(ARM_ENV, raising=False)
    sent = await _ready_canary_requests(_contract_handler())
    arm = sent[-1]
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert arm.mode is EnumPrLandingGithubMode.ENFORCE


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        (MODE_ENV, "loud", "not one of dry_run or enforce"),
        (MODE_ENV, "", "not one of dry_run or enforce"),
        (ARM_ENV, "maybe", "arm_policy"),
        (ARM_ENV, "", "arm_policy"),
    ],
)
def test_overlay_a_malformed_value_fails_at_contract_load(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, fragment: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(PrLandingContractConfigError, match=fragment):
        load_contract_config()
    with pytest.raises(PrLandingContractConfigError, match=fragment):
        _contract_handler()


def test_overlay_refs_expand_in_a_block_and_an_unbound_ref_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_TEST_UNBOUND_MODE", raising=False)
    monkeypatch.setenv("ONEX_TEST_BOUND_MODE", "enforce")
    config = config_from_block(
        {
            "github_mode": "${env.ONEX_TEST_UNBOUND_MODE:dry_run}",
            "github_mode_by_repository": {OTHER: "${env.ONEX_TEST_BOUND_MODE}"},
        }
    )
    assert config.github_mode is EnumPrLandingGithubMode.DRY_RUN
    assert config.github_mode_for(OTHER) is EnumPrLandingGithubMode.ENFORCE
    with pytest.raises(PrLandingContractConfigError, match="github_mode is ''"):
        config_from_block({"github_mode": "${env.ONEX_TEST_UNBOUND_MODE}"})
