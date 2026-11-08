# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 18 (the customer path): generated golden and error chains and the whole-flow run (OMN-22985).

Rebuild piece 18 is the clean-VM onboarding walk: a clean VM fixture goes from install to
a first delegated result, and the weekly reading says whether the walk still ends there.

* The scenarios are generated from ``node_onboarding_clean_vm_walk_effect``'s own
  ``contract.yaml``: its documented steps give the failing legs (one error chain per
  documented step, plus the two log-level legs a passing script can still hide), and its
  ``runtime_dispatch`` gives the command, success and failure topics. A contract change
  that adds or removes a step moves the test.
* Every scenario crosses the in-memory bus: the request travels on the command topic,
  the node answers on the success topic when the receipt passed and on the failure topic
  when it did not, and the weekly reading is computed from that receipt.
* The golden whole-flow run takes the clean VM fixture from install to a first delegated
  result. The error whole-flow run types each failing leg: the receipt names the leg in
  its failure cause, the failure topic carries it, the VM is cleaned up, and the reading
  is NOT_WORKING.
* A named mutation patches one function of the node. A chain test that stays green under
  its mutation cannot fail, so each mutation is run against its unbroken control first and
  must turn its named scenario red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers import (
    handler_onboarding_clean_vm_walk as walk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers import (
    handler_walk_reading as reading,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumWalkOutcome,
    EnumWalkStepKind,
    ModelDelegatedRun,
    ModelRunnerResult,
    ModelWalkReceipt,
    ModelWalkRequest,
    ModelWalkStep,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_walk_reading import (
    ModelWalkReading,
    ModelWalkReadingRequest,
)
from tests.nodes.node_onboarding_clean_vm_walk.test_clean_vm_walk import (
    FAILED_LOG,
    SUCCESS_LOG,
    StubTartRunner,
    request,
)

pytestmark = pytest.mark.unit

CONTRACT: Final = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_onboarding_clean_vm_walk_effect"
    / "contract.yaml"
)
EIGHT_DAYS: Final = 8 * 24 * 3600

#: The two legs a passing script can still hide, beside the documented steps.
LEG_PHASE_FAILED: Final = "onboarding_phase_failed"
LEG_NO_DELEGATED_RUN: Final = "no_delegated_run"

#: The failure cause each leg must carry, when it is not ``<step kind>: exit``.
CAUSE_OVERRIDES: Final[dict[str, str]] = {
    str(EnumWalkStepKind.GUEST_VERIFY_CHECKSUM): "checksum mismatch",
    # ``tart run`` is started detached: it fails by the transport raising, not by an exit.
    str(EnumWalkStepKind.TART_RUN): "leg failure",
    LEG_PHASE_FAILED: "Last error",
    LEG_NO_DELEGATED_RUN: "no successful delegated run in onboarding log",
}


def contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def walk_topics() -> dict[str, str]:
    """The node's command, success and failure topics, read from its contract."""
    dispatch = contract()["runtime_dispatch"]
    return {
        "command": dispatch["command_topic"],
        "success": dispatch["terminal_events"]["success"],
        "failure": dispatch["terminal_events"]["failure"],
    }


def documented_step_kinds() -> list[str]:
    return [step["kind"] for step in contract()["documented_steps"]]


# ---------------------------------------------------------------- the scenarios
@dataclass(frozen=True)
class Scenario:
    name: str
    leg: str | None  # None is the golden chain; otherwise the failing leg

    @property
    def cause(self) -> str:
        assert self.leg is not None
        return CAUSE_OVERRIDES.get(self.leg, f"{self.leg}: exit")


def generated_scenarios() -> list[Scenario]:
    legs = [*documented_step_kinds(), LEG_PHASE_FAILED, LEG_NO_DELEGATED_RUN]
    return [Scenario("golden", None)] + [Scenario(f"error_{leg}", leg) for leg in legs]


SCENARIOS: Final = generated_scenarios()
ERROR_SCENARIOS: Final = [s for s in SCENARIOS if s.leg is not None]
GOLDEN: Final = SCENARIOS[0]


class StartRefusedRunner(StubTartRunner):
    """The Tart host refuses to start the detached VM."""

    def start(self, argv: Sequence[str]) -> None:
        self.calls.append(tuple(argv))
        raise OSError("leg failure")


def stub_for(scenario: Scenario) -> StubTartRunner:
    """A clean VM fixture that either walks to a delegated result or fails one leg."""
    runner = StubTartRunner()
    leg = scenario.leg
    if leg is None:
        return runner
    if leg == LEG_PHASE_FAILED:
        runner.log = FAILED_LOG
        runner.scripted[EnumWalkStepKind.GUEST_RUN_SCRIPT] = [
            ModelRunnerResult(exit_code=1, stdout=FAILED_LOG, stderr="")
        ]
        return runner
    if leg == LEG_NO_DELEGATED_RUN:
        runner.log = SUCCESS_LOG.replace('"status":"success"', '"status":"failed"')
        return runner
    if leg == str(EnumWalkStepKind.TART_RUN):
        return StartRefusedRunner()
    runner.scripted[EnumWalkStepKind(leg)] = [
        ModelRunnerResult(exit_code=1, stdout="", stderr="leg failure")
    ]
    return runner


# ------------------------------------------------------------------ the bus leg
@dataclass
class Carried:
    receipt: ModelWalkReceipt
    topics: list[str]
    answered_on: str
    runner: StubTartRunner


async def walk_over_bus(runner: StubTartRunner) -> Carried:
    """Send the request on the command topic; read the node's terminal off the bus."""
    topics = walk_topics()
    bus = EventBusInmemory(environment="piece18-chain", group="piece18-chain")
    carried: list[str] = []
    answered: asyncio.Future[tuple[str, bytes]] = (
        asyncio.get_running_loop().create_future()
    )

    async def send(topic: str, body: bytes) -> None:
        carried.append(topic)
        await bus.publish(topic, None, body)

    async def on_command(message: Any) -> None:
        wanted = ModelWalkRequest.model_validate_json(message.value)
        receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(wanted)
        topic = (
            topics["success"]
            if receipt.outcome == EnumWalkOutcome.PASSED
            else topics["failure"]
        )
        await send(topic, receipt.model_dump_json().encode())

    async def on_terminal(message: Any) -> None:
        topic = carried[-1]
        if not answered.done():
            answered.set_result((topic, message.value))

    await bus.start()
    try:
        await bus.subscribe(
            topics["command"], on_message=on_command, group_id="walk-node"
        )
        for terminal in (topics["success"], topics["failure"]):
            await bus.subscribe(terminal, on_message=on_terminal, group_id="walker")
        await send(topics["command"], request().model_dump_json().encode())
        topic, raw = await asyncio.wait_for(answered, 20)
    finally:
        await bus.close()
    return Carried(
        receipt=ModelWalkReceipt.model_validate_json(raw),
        topics=carried,
        answered_on=topic,
        runner=runner,
    )


def weekly_reading(receipt: ModelWalkReceipt) -> ModelWalkReading:
    finished = datetime.fromisoformat(receipt.finished_at)
    return reading.HandlerWalkReading().handle(
        ModelWalkReadingRequest(
            receipt=receipt,
            now=finished + timedelta(hours=1),
            max_age_seconds=EIGHT_DAYS,
        )
    )


def issued_kinds(runner: StubTartRunner) -> list[EnumWalkStepKind]:
    return [runner.kind(argv) for argv in runner.calls if argv[-1] != "--version"]


# --------------------------------------------------------------- the assertions
def assert_golden(done: Carried) -> None:
    topics = walk_topics()
    receipt = done.receipt
    assert done.topics == [topics["command"], topics["success"]], done.topics
    assert done.answered_on == topics["success"]
    assert receipt.outcome == EnumWalkOutcome.PASSED, receipt.failure_text
    assert receipt.terminal_failure_cause is None
    # Install to first delegated result: every documented step ran, in contract order.
    assert [str(k) for k in issued_kinds(done.runner)] == documented_step_kinds()
    assert receipt.delegated_run.status == "success"
    assert receipt.delegated_run.run_id
    assert receipt.seconds_to_first_delegated_run is not None
    assert receipt.vm_deleted
    assert receipt.dashboard.root_http_status == 200
    assert receipt.dashboard.projections_http_status == 200
    verdict = weekly_reading(receipt)
    assert verdict.verdict == EnumReadingVerdict.WORKING, verdict.reasons
    assert verdict.ends_in_delegated_result


def assert_error(scenario: Scenario, done: Carried) -> None:
    topics = walk_topics()
    receipt = done.receipt
    assert done.topics == [topics["command"], topics["failure"]], done.topics
    assert done.answered_on == topics["failure"]
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert receipt.model_dump(mode="json")["status"] == "failed"
    assert scenario.cause in (receipt.terminal_failure_cause or ""), (
        scenario.leg,
        receipt.terminal_failure_cause,
    )
    assert receipt.terminal_failure_cause == receipt.failure_text
    if scenario.leg in documented_step_kinds():
        # The failing leg is typed in the receipt's own step records.
        records = [r for r in receipt.steps if str(r.kind) == scenario.leg]
        assert records
        assert all(r.exit_code != 0 for r in records), records
    kinds = issued_kinds(done.runner)
    if scenario.leg == str(EnumWalkStepKind.TART_CLONE):
        # A refused clone created nothing, so there is nothing of this walk to delete.
        assert not receipt.vm_deleted
        assert EnumWalkStepKind.TART_DELETE not in kinds
    elif scenario.leg == str(EnumWalkStepKind.TART_DELETE):
        assert not receipt.vm_deleted
        assert "left undeleted" in (receipt.failure_text or "")
    else:
        assert receipt.vm_deleted
        assert kinds[-1] == EnumWalkStepKind.TART_DELETE
    verdict = weekly_reading(receipt)
    assert verdict.verdict == EnumReadingVerdict.NOT_WORKING, verdict.reasons
    assert reading.REASON_NOT_DELEGATED in verdict.reasons
    assert not verdict.ends_in_delegated_result


async def run_scenario(scenario: Scenario) -> Carried:
    done = await walk_over_bus(stub_for(scenario))
    if scenario.leg is None:
        assert_golden(done)
    else:
        assert_error(scenario, done)
    return done


# ------------------------------------------------------------ the named mutations
@contextmanager
def _patched(target: object, name: str, replacement: object) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, replacement)
    try:
        yield
    finally:
        setattr(target, name, original)


def mutation_failures_swallowed() -> AbstractContextManager[None]:
    """A failing step no longer stops the walk."""
    return _patched(walk, "_require_success", lambda _result, _kind: None)


def mutation_checksum_unchecked() -> AbstractContextManager[None]:
    """A checksum mismatch passes."""
    real = walk.DocumentedStepGuard.issue

    def issue(self: Any, step: ModelWalkStep) -> ModelRunnerResult:
        result = real(self, step)
        if step.kind == EnumWalkStepKind.GUEST_VERIFY_CHECKSUM:
            return ModelRunnerResult(exit_code=0, stdout="", stderr="")
        return result

    return _patched(walk.DocumentedStepGuard, "issue", issue)


def mutation_delegated_run_assumed() -> AbstractContextManager[None]:
    """The walk reads a delegated result the log does not hold."""
    return _patched(
        walk,
        "parse_delegated_run",
        lambda _log: ModelDelegatedRun(
            backend="local", status="success", run_id="assumed"
        ),
    )


def mutation_failure_reported_as_pass() -> AbstractContextManager[None]:
    """A failed walk is published as a passing receipt."""
    real = walk.sanitize_receipt

    def sanitize(receipt: ModelWalkReceipt) -> ModelWalkReceipt:
        return real(
            receipt.model_copy(
                update={
                    "outcome": EnumWalkOutcome.PASSED,
                    "status": "passed",
                    "failure_text": None,
                    "terminal_failure_cause": None,
                }
            )
        )

    return _patched(walk, "sanitize_receipt", sanitize)


def mutation_cleanup_never_deletes() -> AbstractContextManager[None]:
    """The delete step reports success without reaching the VM host."""
    real = walk.DocumentedStepGuard.issue

    def issue(self: Any, step: ModelWalkStep) -> ModelRunnerResult:
        if step.kind == EnumWalkStepKind.TART_DELETE:
            return ModelRunnerResult(exit_code=0, stdout="", stderr="")
        return real(self, step)

    return _patched(walk.DocumentedStepGuard, "issue", issue)


def mutation_reading_ignores_failed_walk() -> AbstractContextManager[None]:
    """The weekly reading counts a failed walk as ending in a delegated result."""
    return _patched(reading, "walk_ends_in_delegated_result", lambda _receipt: True)


#: name -> (the break, the scenario that must turn red under it)
NAMED_MUTATIONS: Final[
    dict[str, tuple[Callable[[], AbstractContextManager[None]], str]]
] = {
    "failures_swallowed": (
        mutation_failures_swallowed,
        f"error_{EnumWalkStepKind.TART_SET_SPEC}",
    ),
    "checksum_unchecked": (
        mutation_checksum_unchecked,
        f"error_{EnumWalkStepKind.GUEST_VERIFY_CHECKSUM}",
    ),
    "delegated_run_assumed": (
        mutation_delegated_run_assumed,
        f"error_{LEG_NO_DELEGATED_RUN}",
    ),
    "failure_reported_as_pass": (
        mutation_failure_reported_as_pass,
        f"error_{EnumWalkStepKind.TART_RUN}",
    ),
    "cleanup_never_deletes": (mutation_cleanup_never_deletes, "golden"),
    "reading_ignores_failed_walk": (
        mutation_reading_ignores_failed_walk,
        f"error_{EnumWalkStepKind.GUEST_RUN_SCRIPT}",
    ),
}

BY_NAME: Final = {s.name: s for s in SCENARIOS}


# ------------------------------------------------------------------- the tests
def test_scenarios_are_generated_from_the_contract() -> None:
    kinds = documented_step_kinds()
    assert set(kinds) == {str(k) for k in EnumWalkStepKind}
    assert [s.leg for s in ERROR_SCENARIOS] == [
        *kinds,
        LEG_PHASE_FAILED,
        LEG_NO_DELEGATED_RUN,
    ]
    assert walk_topics()["success"] != walk_topics()["failure"]


async def test_golden_takes_a_clean_vm_from_install_to_a_first_delegated_result() -> (
    None
):
    done = await run_scenario(GOLDEN)
    assert done.receipt.delegated_run.status == "success"


@pytest.mark.parametrize("scenario", ERROR_SCENARIOS, ids=lambda s: s.name)
async def test_error_types_each_failing_leg(scenario: Scenario) -> None:
    done = await run_scenario(scenario)
    assert done.receipt.outcome == EnumWalkOutcome.FAILED


async def test_whole_flow_golden_and_error_run_writes_a_receipt(tmp_path: Path) -> None:
    """The whole flow: one golden run and every failing leg, recorded in one receipt."""
    topics = walk_topics()
    runs: dict[str, object] = {}
    for scenario in SCENARIOS:
        done = await run_scenario(scenario)
        runs[scenario.name] = {
            "topics": done.topics,
            "outcome": str(done.receipt.outcome),
            "cause": done.receipt.terminal_failure_cause,
            "vm_deleted": done.receipt.vm_deleted,
            "reading": weekly_reading(done.receipt).verdict.value,
        }
    path = tmp_path / "piece18_whole_flow_receipt.json"
    path.write_text(json.dumps({"topics": topics, "runs": runs}, indent=2))
    written = json.loads(path.read_text())
    assert len(written["runs"]) == len(SCENARIOS)
    assert written["runs"]["golden"]["reading"] == EnumReadingVerdict.WORKING.value
    for scenario in ERROR_SCENARIOS:
        run = written["runs"][scenario.name]
        assert run["outcome"] == "failed", scenario.name
        assert run["reading"] == EnumReadingVerdict.NOT_WORKING.value, scenario.name


@pytest.mark.parametrize("name", sorted(NAMED_MUTATIONS))
async def test_mutation_turns_its_scenario_red_after_a_green_control(name: str) -> None:
    mutation, scenario_name = NAMED_MUTATIONS[name]
    scenario = BY_NAME[scenario_name]
    await run_scenario(scenario)  # the unbroken control is green
    with mutation(), pytest.raises(AssertionError):
        await run_scenario(scenario)
    await run_scenario(scenario)  # and green again once the break is lifted


def test_mutation_names_cover_golden_and_error_chains() -> None:
    reds = {scenario for _, scenario in NAMED_MUTATIONS.values()}
    assert "golden" in reds
    assert any(name.startswith("error_") for name in reds)
