# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_fsm_single_source_check_compute: the contract is the only source of a state machine (OMN-21477)."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

import omnimarket
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
    run_local,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_fsm_single_source_check_compute import (
    NodeFsmSingleSourceCheckCompute,
)
from omnimarket.nodes.node_fsm_single_source_check_compute.handlers import (
    handler_fsm_single_source_check,
)
from omnimarket.nodes.node_fsm_single_source_check_compute.handlers.handler_fsm_single_source_check import (
    CONTRACT_PATH,
    HandlerFsmSingleSourceCheck,
)
from omnimarket.nodes.node_fsm_single_source_check_compute.models.model_fsm_single_source_check import (
    EnumSingleSourceIssue,
    ModelFsmSingleSourceCheckRequest,
    ModelFsmSingleSourceCheckResult,
    ModelSourceText,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
PACKAGE_ROOT = Path(omnimarket.__file__).resolve().parent


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text("utf-8")


def _check(
    source_name: str, contract: str = "contract_machine.yaml"
) -> ModelFsmSingleSourceCheckResult:
    request = ModelFsmSingleSourceCheckRequest(
        contract_text=_fixture(contract),
        sources=(ModelSourceText(name=source_name, text=_fixture(source_name)),),
    )
    return HandlerFsmSingleSourceCheck().handle(request)


def _codes(result: ModelFsmSingleSourceCheckResult) -> set[EnumSingleSourceIssue]:
    return {issue.code for issue in result.issues}


def test_state_trigger_mapping_fails_and_names_the_line() -> None:
    result = _check("reducer_allowed_transitions.py.txt")
    assert result.exit_code != 0
    table = [i for i in result.issues if i.code == EnumSingleSourceIssue.STATE_TABLE]
    assert [i.line for i in table] == [5]
    assert table[0].source == "reducer_allowed_transitions.py.txt"
    assert "line 5" in table[0].message


def test_reducer_routed_through_execute_transition_passes() -> None:
    result = _check("reducer_clean.py.txt")
    assert result.issues == ()
    assert result.exit_code == 0
    assert result.state_names == ("drafted", "built", "done", "refused")


def test_function_local_mapping_beside_execute_transition_fails() -> None:
    result = _check("handler_local_mapping.py.txt")
    assert _codes(result) == {EnumSingleSourceIssue.STATE_TABLE}
    assert [i.line for i in result.issues] == [7]


@pytest.mark.parametrize(
    ("source", "line"),
    [("state_enum.py.txt", 8), ("state_tuple.py.txt", 5)],
)
def test_state_only_enum_or_tuple_fails(source: str, line: int) -> None:
    result = _check(source)
    assert [(i.code, i.line) for i in result.issues] == [
        (EnumSingleSourceIssue.STATE_TABLE, line)
    ]


def test_branch_chain_on_state_literals_fails() -> None:
    result = _check("branch_chain.py.txt")
    assert _codes(result) == {EnumSingleSourceIssue.STATE_BRANCH_CHAIN}
    assert [i.line for i in result.issues] == [8]


def test_single_state_name_in_a_collection_is_not_a_table() -> None:
    source = 'KEYS = {"drafted": 1, "other": 2}\nexecute_transition(a, b, c, d)\n'
    request = ModelFsmSingleSourceCheckRequest(
        contract_text=_fixture("contract_machine.yaml"),
        sources=(ModelSourceText(name="one.py", text=source),),
    )
    assert HandlerFsmSingleSourceCheck().handle(request).issues == ()


def test_handler_that_never_calls_execute_transition_fails() -> None:
    result = _check("no_executor.py.txt")
    assert _codes(result) == {EnumSingleSourceIssue.EXECUTE_TRANSITION_MISSING}


def test_contract_without_state_machine_fails() -> None:
    result = _check("reducer_clean.py.txt", contract="contract_without_machine.yaml")
    assert _codes(result) == {EnumSingleSourceIssue.STATE_MACHINE_MISSING}
    assert result.exit_code != 0


def test_unparseable_source_and_contract_are_issues_not_crashes() -> None:
    request = ModelFsmSingleSourceCheckRequest(
        contract_text="state_machine: [unclosed",
        sources=(ModelSourceText(name="broken.py", text="def (:\n"),),
    )
    result = HandlerFsmSingleSourceCheck().handle(request)
    assert EnumSingleSourceIssue.CONTRACT_UNREADABLE in _codes(result)
    request = ModelFsmSingleSourceCheckRequest(
        contract_text=_fixture("contract_machine.yaml"),
        sources=(ModelSourceText(name="broken.py", text="def (:\n"),),
    )
    result = HandlerFsmSingleSourceCheck().handle(request)
    assert EnumSingleSourceIssue.SOURCE_UNPARSEABLE in _codes(result)


# --- the reducers of the plan that exist -------------------------------------------------


@dataclass(frozen=True)
class CheckedReducer:
    """A reducer of the plan: its node and the shared modules that run its machine."""

    node: str
    shared: tuple[str, ...] = ()


# Each later reducer task adds its node here in its own PR and must pass.
CHECKED_REDUCERS: tuple[CheckedReducer, ...] = (
    CheckedReducer("node_design_process_reducer"),
    CheckedReducer(
        "node_wip_gate_reducer",
        shared=("handlers/rules_wip_gate.py", "models/wip_gate.py"),
    ),
)


def _request_for(reducer: CheckedReducer) -> ModelFsmSingleSourceCheckRequest:
    node_dir = PACKAGE_ROOT / "nodes" / reducer.node
    paths = sorted(node_dir.rglob("*.py")) + [
        PACKAGE_ROOT / part for part in reducer.shared
    ]
    return ModelFsmSingleSourceCheckRequest(
        contract_text=(node_dir / "contract.yaml").read_text("utf-8"),
        sources=tuple(
            ModelSourceText(
                name=str(path.relative_to(PACKAGE_ROOT)), text=path.read_text("utf-8")
            )
            for path in paths
        ),
    )


@pytest.mark.parametrize("reducer", CHECKED_REDUCERS, ids=lambda r: r.node)
def test_reducers_of_the_plan_pass(reducer: CheckedReducer) -> None:
    result = HandlerFsmSingleSourceCheck().handle(_request_for(reducer))
    assert result.issues == ()
    assert len(result.state_names) >= 2


def test_a_listed_reducer_without_state_machine_fails() -> None:
    request = _request_for(CHECKED_REDUCERS[0]).model_copy(
        update={"contract_text": _fixture("contract_without_machine.yaml")}
    )
    result = HandlerFsmSingleSourceCheck().handle(request)
    assert EnumSingleSourceIssue.STATE_MACHINE_MISSING in _codes(result)


def test_every_listed_reducer_declares_a_state_machine_in_its_contract() -> None:
    for reducer in CHECKED_REDUCERS:
        contract = yaml.safe_load(
            (PACKAGE_ROOT / "nodes" / reducer.node / "contract.yaml").read_text("utf-8")
        )
        assert contract["node_type"].startswith("REDUCER"), reducer.node
        assert "state_machine" in contract, reducer.node


# --- configuration and bindings ----------------------------------------------------------


def test_request_takes_the_two_texts_and_nothing_else() -> None:
    assert set(ModelFsmSingleSourceCheckRequest.model_fields) == {
        "contract_text",
        "sources",
    }
    assert set(ModelSourceText.model_fields) == {"name", "text"}


def test_handler_reads_no_environment_and_no_file() -> None:
    source = inspect.getsource(handler_fsm_single_source_check)
    for forbidden in ("os.environ", "os.getenv", "getenv", "read_text", "open("):
        assert forbidden not in source.replace("CONTRACT_PATH", ""), forbidden
    assert not inspect.signature(HandlerFsmSingleSourceCheck).parameters


def test_no_protocol_dependencies_in_contract() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))
    assert "protocol_dependencies" not in contract
    assert contract["node_type"] == "compute"
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.fsm-single-source-checked.v1"
    )


def test_in_process_entry_wrapper_and_local_bus_entry_are_byte_identical() -> None:
    request = ModelFsmSingleSourceCheckRequest(
        contract_text=_fixture("contract_machine.yaml"),
        sources=tuple(
            ModelSourceText(name=name, text=_fixture(name))
            for name in (
                "reducer_allowed_transitions.py.txt",
                "handler_local_mapping.py.txt",
                "state_enum.py.txt",
                "branch_chain.py.txt",
                "no_executor.py.txt",
            )
        ),
    )
    direct = HandlerFsmSingleSourceCheck().handle(request).model_dump_json()
    wrapped = NodeFsmSingleSourceCheckCompute().handle(request).model_dump_json()
    local = asyncio.run(
        run_local(
            "node_fsm_single_source_check_compute",
            "check_fsm_single_source",
            request.model_dump(mode="json"),
            ModelRunLocalConfig(
                command_topic="onex.cmd.omnimarket.fsm-single-source-check-requested.v1",
                result_topic="onex.evt.omnimarket.fsm-single-source-checked.v1",
                consumer_group="test-fsm-single-source",
                timeout_seconds=10.0,
            ),
        )
    )
    assert direct == wrapped
    assert (
        ModelFsmSingleSourceCheckResult.model_validate(local).model_dump_json()
        == direct
    )
    assert "protocol_dependencies" not in yaml.safe_load(
        CONTRACT_PATH.read_text("utf-8")
    )


def test_a_stub_that_changes_a_verdict_breaks_the_comparison() -> None:
    request = ModelFsmSingleSourceCheckRequest(
        contract_text=_fixture("contract_machine.yaml"),
        sources=(
            ModelSourceText(
                name="a.py", text=_fixture("reducer_allowed_transitions.py.txt")
            ),
        ),
    )
    real = HandlerFsmSingleSourceCheck().handle(request)
    stub = real.model_copy(update={"exit_code": 0, "issues": ()})
    assert real.model_dump_json() != stub.model_dump_json()
