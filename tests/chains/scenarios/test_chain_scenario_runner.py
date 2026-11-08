# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The chain scenario runner's controls on fixture handlers (OMN-19998).

Each fault kind, silence, a bare retry and each named break has a case that must be red
for the stated reason. The positive control is the same fixture with the fault removed.
Cases are built from the typed core models, not typed by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_chain_scenario_binding_role import (
    EnumChainScenarioBindingRole,
)
from omnibase_core.enums.enum_chain_scenario_binding_target import (
    EnumChainScenarioBindingTarget,
)
from omnibase_core.enums.enum_chain_scenario_break import EnumChainScenarioBreak
from omnibase_core.enums.enum_chain_scenario_fault_kind import (
    EnumChainScenarioFaultKind,
)
from omnibase_core.enums.enum_chain_scenario_kind import EnumChainScenarioKind
from omnibase_core.enums.enum_chain_scenario_oracle_basis import (
    EnumChainScenarioOracleBasis,
)
from omnibase_core.enums.enum_chain_scenario_reading import EnumChainScenarioReading
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_binding import (
    ModelChainScenarioBinding,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_case import (
    ModelChainScenarioCase,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_expected_event import (
    ModelChainScenarioExpectedEvent,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_failure_event import (
    ModelChainScenarioFailureEvent,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_fault import (
    ModelChainScenarioFault,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_input import (
    ModelChainScenarioInput,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_ledger_row import (
    ModelChainScenarioLedgerRow,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_oracle import (
    ModelChainScenarioOracle,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_state_update import (
    ModelChainScenarioStateUpdate,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_subcontract import (
    ModelChainScenarioSubcontract,
)
from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.chain_scenarios.assertions import assert_scenario
from omnimarket.handlers.chain_scenarios.errors import (
    ChainScenarioAssertionError,
    EnumChainAssertionCode,
)
from omnimarket.handlers.chain_scenarios.model_chain_scenario_overlay import (
    load_overlay,
)
from omnimarket.handlers.chain_scenarios.runner import ChainScenarioNode

pytestmark = pytest.mark.unit

_IN = "onex.cmd.fixture.record.v1"
_DONE = "onex.evt.fixture.recorded.v1"
_F = "tests.chains.scenarios.fixture_handlers"
_SUBJECT = "node_fixture_record_effect"
_BROKER_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/handlers/chain_scenarios/overlays/chain_scenario.redpanda.overlay.yaml"
)
_K = EnumChainScenarioFaultKind
_B = EnumChainScenarioBreak


class _BusSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subscribe_topics: tuple[str, ...]
    publish_topics: tuple[str, ...]


class _FixtureContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    handler: dict[str, str]
    event_bus: _BusSpec
    chain_scenarios: ModelChainScenarioSubcontract


def _input() -> ModelChainScenarioInput:
    return ModelChainScenarioInput(
        topic=_IN, payload={"row": "alpha"}, correlation_field="correlation_id"
    )


def _oracle() -> ModelChainScenarioOracle:
    return ModelChainScenarioOracle(
        basis=EnumChainScenarioOracleBasis.STATED_BY_PERSON,
        statement="stated for the fixture",
    )


def _golden() -> ModelChainScenarioCase:
    return ModelChainScenarioCase(
        case_name="golden",
        kind=EnumChainScenarioKind.GOLDEN,
        input=_input(),
        expected_events=(
            ModelChainScenarioExpectedEvent(topic=_DONE, fields={"accepted": True}),
        ),
        state_updates=(
            ModelChainScenarioStateUpdate(binding="store", target="rows", key="alpha"),
        ),
        oracle=_oracle(),
        breaks=(
            _B.DROP_TERMINAL_PUBLISH,
            _B.WRONG_CORRELATION_ID,
            _B.SKIPPED_REDUCER_UPDATE,
        ),
    )


def _error(fault: ModelChainScenarioFault) -> ModelChainScenarioCase:
    return ModelChainScenarioCase(
        case_name="error",
        kind=EnumChainScenarioKind.ERROR,
        input=_input(),
        fault=fault,
        expected_events=(
            ModelChainScenarioExpectedEvent(
                topic=_DONE, fields={"accepted": False, "reason": "sink_down"}
            ),
        ),
        failure="sink_down",
        ledger_failure_row=ModelChainScenarioLedgerRow(
            row_type="FRICTION", subject=_SUBJECT
        ),
        reading=EnumChainScenarioReading.NOT_WORKING,
        oracle=_oracle(),
        breaks=(_B.DROP_TERMINAL_PUBLISH, _B.WRONG_CORRELATION_ID),
    )


_DOWN = ModelChainScenarioFault(kind=_K.DEPENDENCY_DOWN, boundary="sink")


def _node(
    tmp_path: Path, handler: str, cases: tuple[ModelChainScenarioCase, ...]
) -> ChainScenarioNode:
    scenario = ModelChainScenarioSubcontract(
        schema_version=1,
        success_topic=_DONE,
        failures=(
            ModelChainScenarioFailureEvent(
                name="sink_down", topic=_DONE, discriminators={"accepted": False}
            ),
        ),
        bindings=(
            ModelChainScenarioBinding(
                name="sink",
                target=EnumChainScenarioBindingTarget.CONSTRUCTOR_KWARG,
                attribute="sink",
                in_memory_factory=f"{_F}:sink",
                fault_factories={
                    _K.DEPENDENCY_DOWN: f"{_F}:down_sink",
                    _K.MISSED_DEADLINE: f"{_F}:slow_sink",
                },
            ),
            ModelChainScenarioBinding(
                name="store",
                target=EnumChainScenarioBindingTarget.CONSTRUCTOR_KWARG,
                attribute="store",
                role=EnumChainScenarioBindingRole.REDUCER_STATE,
                in_memory_factory=f"{_F}:store",
            ),
        ),
        cases=cases,
    )
    contract = _FixtureContract(
        name=_SUBJECT,
        handler={"module": _F, "class": handler},
        event_bus=_BusSpec(subscribe_topics=(_IN,), publish_topics=(_DONE,)),
        chain_scenarios=scenario,
    )
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract.model_dump(mode="json")), encoding="utf-8")
    return ChainScenarioNode(path, load_overlay())


async def _outcome(
    node: ChainScenarioNode,
    case_name: str,
    injected_break: EnumChainScenarioBreak | None = None,
) -> EnumChainAssertionCode | None:
    case = next(c for c in node.scenario.cases if c.case_name == case_name)
    run = await node.run(case, injected_break)
    try:
        assert_scenario(run, case, node.scenario)
    except ChainScenarioAssertionError as exc:
        return exc.code
    return None


async def test_the_positive_controls_pass(tmp_path: Path) -> None:
    node = _node(tmp_path, "HandlerTyped", (_golden(), _error(_DOWN)))
    assert await _outcome(node, "golden") is None
    assert await _outcome(node, "error") is None


@pytest.mark.parametrize(
    ("injected_break", "code"),
    [
        (_B.DROP_TERMINAL_PUBLISH, EnumChainAssertionCode.SILENCE),
        (_B.WRONG_CORRELATION_ID, EnumChainAssertionCode.CORRELATION),
        (_B.SKIPPED_REDUCER_UPDATE, EnumChainAssertionCode.STATE_UPDATE),
    ],
)
async def test_each_named_break_turns_the_golden_case_red(
    tmp_path: Path, injected_break: EnumChainScenarioBreak, code: EnumChainAssertionCode
) -> None:
    node = _node(tmp_path, "HandlerTyped", (_golden(),))
    assert await _outcome(node, "golden", injected_break) is code


async def test_the_correlation_break_rewrites_a_payload_carried_id(
    tmp_path: Path,
) -> None:
    node = _node(tmp_path, "HandlerTyped", (_golden(),))
    run = await node.run(node.scenario.cases[0], _B.WRONG_CORRELATION_ID)
    assert [e.correlation_source for e in run.events] == ["payload"]
    assert run.events[0].correlation_id != run.correlation_id


async def test_a_dependency_down_fault_ends_in_the_typed_failure_row_and_reading(
    tmp_path: Path,
) -> None:
    node = _node(tmp_path, "HandlerTyped", (_error(_DOWN),))
    run = await node.run(node.scenario.cases[0])
    assert [e.failure for e in run.events] == ["sink_down"]
    assert [(r.row_type, r.subject) for r in run.ledger_rows] == [
        ("FRICTION", _SUBJECT)
    ]
    assert run.readings == {_SUBJECT: EnumChainScenarioReading.NOT_WORKING}
    assert run.dependency_calls["sink"] == 1


async def test_a_bare_retry_is_red(tmp_path: Path) -> None:
    node = _node(tmp_path, "HandlerBareRetry", (_error(_DOWN),))
    assert await _outcome(node, "error") is EnumChainAssertionCode.BARE_RETRY


async def test_a_handler_that_raises_is_silence_and_red(tmp_path: Path) -> None:
    node = _node(tmp_path, "HandlerRaises", (_error(_DOWN),))
    assert await _outcome(node, "error") is EnumChainAssertionCode.SILENCE


async def test_an_injected_handler_error_is_silence_and_red(tmp_path: Path) -> None:
    fault = ModelChainScenarioFault(kind=_K.HANDLER_ERROR)
    node = _node(tmp_path, "HandlerTyped", (_error(fault),))
    assert await _outcome(node, "error") is EnumChainAssertionCode.SILENCE


async def test_a_missed_deadline_with_no_failure_event_is_silence_and_red(
    tmp_path: Path,
) -> None:
    fault = ModelChainScenarioFault(
        kind=_K.MISSED_DEADLINE, boundary="sink", deadline_ms=50
    )
    node = _node(tmp_path, "HandlerTyped", (_error(fault),))
    run = await node.run(node.scenario.cases[0])
    assert run.timed_out is True
    assert await _outcome(node, "error") is EnumChainAssertionCode.SILENCE


async def test_a_golden_case_whose_run_reports_a_declared_failure_is_red(
    tmp_path: Path,
) -> None:
    golden = _golden().model_copy(
        update={
            "expected_events": (
                ModelChainScenarioExpectedEvent(
                    topic=_DONE, fields={"accepted": False, "reason": "sink_down"}
                ),
            ),
            "state_updates": (),
        }
    )
    failing = _node(tmp_path, "HandlerTyped", (_error(_DOWN),))
    failing_run = await failing.run(failing.scenario.cases[0])
    with pytest.raises(ChainScenarioAssertionError) as raised:
        assert_scenario(failing_run, golden, failing.scenario)
    assert raised.value.code is EnumChainAssertionCode.GOLDEN_ENDS_IN_FAILURE


def test_the_packaged_overlay_is_the_in_memory_bus() -> None:
    overlay = load_overlay()
    assert overlay.bus == "inmemory"
    assert overlay.backend_overrides() == {"event_bus": "inmemory"}


def test_the_broker_overlay_needs_its_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = load_overlay(_BROKER_OVERLAY)
    assert overlay.bus == "kafka"
    assert overlay.kafka_bootstrap_env is not None
    monkeypatch.delenv(overlay.kafka_bootstrap_env, raising=False)
    with pytest.raises(Exception, match="has no instance to run against"):
        overlay.backend_overrides()
    monkeypatch.setenv(overlay.kafka_bootstrap_env, "drill-broker:9092")
    assert overlay.backend_overrides() == {
        "event_bus": "kafka",
        "kafka_bootstrap": "drill-broker:9092",
    }
