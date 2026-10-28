# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parity run: the contract's graph, the comparison verdicts, the overlay, run_local."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.contracts.model_contract_workflow_definition import (
    ModelContractWorkflowDefinition,
)
from pydantic import JsonValue, ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_parity_run import (
    HandlerParityRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
    run_local,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelParityRequest,
    ModelParityScenario,
    ModelScenarioRow,
    ModelScenarioRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
    load_factory_bindings,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_binding_parity_orchestrator"
)
FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 10, 28, 6, 0, 0, tzinfo=UTC)


def _run(
    profile: str,
    keys: tuple[str, ...] = ("a", "b", "c"),
    *,
    binding: str = "",
    contents: dict[str, str] | None = None,
    counts: dict[str, int] | None = None,
    lines: tuple[str, ...] = (),
    provenance: dict[str, str] | None = None,
) -> ModelScenarioRun:
    return ModelScenarioRun(
        profile_id=profile,
        binding=binding or profile,
        proof_eligible=True,
        verdicts=("PASS",),
        lines=lines,
        rows=tuple(
            ModelScenarioRow(
                key=k,
                content=(contents or {}).get(k, f"row-{k}"),
                provenance=provenance or {},
            )
            for k in keys
        ),
        counts=counts or {},
    )


# AC1 --------------------------------------------------------------------------------------


def test_execution_graph_equals_the_fixture_copy_of_the_fragment_and_loads() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    fragment = yaml.safe_load(
        (FIXTURES / "figure9_graph_fragment.yaml").read_text(encoding="utf-8")
    )
    assert contract["workflow_coordination"] == fragment["workflow_coordination"]
    definition = ModelContractWorkflowDefinition.model_validate(
        contract["workflow_coordination"]["workflow_definition"]
    )
    assert [n.node_id for n in definition.execution_graph.nodes] == [
        "contract",
        "overlay",
        "resolve",
        "run_baseline",
        "run_others",
        "parity",
    ]


def test_loaded_graph_drops_step_config_so_the_raw_mapping_is_read() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    workflow = contract["workflow_coordination"]["workflow_definition"]
    definition = ModelContractWorkflowDefinition.model_validate(workflow)
    assert all(
        "step_config" not in node.model_dump()
        for node in definition.execution_graph.nodes
    )
    operations = [
        n.get("step_config", {}).get("operation")
        for n in workflow["execution_graph"]["nodes"]
    ]
    assert operations == [
        None,
        None,
        "resolve_profile_bindings",
        "run_scenario",
        "run_scenario",
        "compare_runs",
    ]


# AC2: five cases, one passing and four failing --------------------------------------------


def test_identical_projections_pass_though_provenance_differs() -> None:
    baseline = _run(
        "alpha", lines=("SEAM x binding=alpha",), provenance={"offset": "1"}
    )
    other = _run("beta", lines=("SEAM x binding=beta",), provenance={"offset": "9"})
    result = compare_runs((baseline, other))
    assert result.passed
    assert result.first_difference == ""


def test_row_content_difference_names_the_first_difference() -> None:
    result = compare_runs((_run("alpha"), _run("beta", contents={"b": "changed"})))
    assert not result.passed
    assert "row content at index 1" in result.first_difference
    assert "'b'" in result.first_difference


def test_row_order_difference_names_the_first_difference() -> None:
    result = compare_runs((_run("alpha"), _run("beta", ("a", "c", "b"))))
    assert not result.passed
    assert "row order at index 1" in result.first_difference


def test_missing_row_names_the_first_difference() -> None:
    result = compare_runs((_run("alpha"), _run("beta", ("a", "c", "x"))))
    assert not result.passed
    assert "missing row 'b'" in result.first_difference


def test_count_difference_names_the_first_difference() -> None:
    result = compare_runs(
        (_run("alpha", counts={"written": 3}), _run("beta", counts={"written": 2}))
    )
    assert not result.passed
    assert "count written: baseline 3, got 2" in result.first_difference


def test_binding_provenance_must_equal_the_profile_that_ran() -> None:
    result = compare_runs((_run("alpha"), _run("beta", binding="alpha")))
    assert not result.passed
    assert "binding='alpha'" in result.first_difference


def test_proof_eligibility_is_compared_unless_the_scenario_allows_the_difference() -> (
    None
):
    other = _run("beta").model_copy(update={"proof_eligible": False})
    assert not compare_runs((_run("alpha"), other)).passed
    assert compare_runs((_run("alpha"), other), ("proof_eligible",)).passed


# AC3 and AC5: the overlay ------------------------------------------------------------------


def _overlay(document: dict[str, object]) -> ModelFactoryBindingsOverlay:
    return ModelFactoryBindingsOverlay.model_validate(document)


def _request(
    overlay: ModelFactoryBindingsOverlay, **fixture: object
) -> ModelParityRequest:
    return ModelParityRequest(
        scenario=ModelParityScenario(
            kind="stub_rows",
            fixture={"topic": "parity.demo", "messages": ["m1", "m2", "m3"], **fixture},
        ),
        overlay=overlay,
        fixed_now=FIXED_NOW,
    )


def _fixture_overlay() -> ModelFactoryBindingsOverlay:
    return _overlay(yaml.safe_load((FIXTURES / "overlay_alpha_beta.yaml").read_text()))


def test_profile_list_without_a_baseline_refuses_naming_the_overlay_key() -> None:
    overlay = _overlay(
        {
            "profiles": [{"id": "alpha"}, {"id": "beta"}],
            "bindings": {"alpha": {}, "beta": {}},
            "proof_bindings": [],
        }
    )
    with pytest.raises(NodeOverlayError, match=r"factory_bindings\.profiles.*baseline"):
        HandlerParityRun().handle(_request(overlay))


def test_profile_list_with_no_other_profile_refuses_naming_the_overlay_key() -> None:
    overlay = _overlay(
        {
            "profiles": [{"id": "alpha", "baseline": True}],
            "bindings": {"alpha": {"BUS": "reference", "CLK": "fixed"}},
            "proof_bindings": [],
        }
    )
    with pytest.raises(NodeOverlayError, match=r"factory_bindings\.profiles.*besides"):
        HandlerParityRun().handle(_request(overlay))


def test_a_profile_missing_a_binding_refuses_naming_the_key() -> None:
    overlay = _overlay(
        {
            "profiles": [{"id": "alpha", "baseline": True}, {"id": "beta"}],
            "bindings": {
                "alpha": {"BUS": "reference", "CLK": "fixed"},
                "beta": {"BUS": "reference"},
            },
            "proof_bindings": [],
        }
    )
    with pytest.raises(
        NodeOverlayError, match=r"factory_bindings\.bindings\.beta\.CLK"
    ):
        HandlerParityRun().handle(_request(overlay))


def test_fixture_overlay_with_profiles_alpha_and_beta_runs_and_walks_the_graph() -> (
    None
):
    result = HandlerParityRun().handle(_request(_fixture_overlay()))
    assert result.passed, result.first_difference
    assert [run.profile_id for run in result.runs] == ["alpha", "beta"]
    assert result.steps == (
        "contract",
        "overlay",
        "resolve",
        "run_baseline",
        "run_others",
        "parity",
    )


def test_changing_the_overlay_moves_the_verdict() -> None:
    document = yaml.safe_load((FIXTURES / "overlay_alpha_beta.yaml").read_text())
    document["bindings"]["beta"]["BUS"] = "out_of_order"
    result = HandlerParityRun().handle(_request(_overlay(document)))
    assert not result.passed


def test_overlay_is_loaded_from_the_overlay_root_by_its_id(tmp_path: Path) -> None:
    target = tmp_path / "factory_bindings"
    target.mkdir()
    (target / "overlay.yaml").write_text(
        (FIXTURES / "overlay_alpha_beta.yaml").read_text()
    )
    loaded = load_factory_bindings({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert [p.id for p in loaded.profiles] == ["alpha", "beta"]
    with pytest.raises(NodeOverlayError, match="factory_bindings"):
        load_factory_bindings({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")})


def test_node_source_has_no_profile_name_literal() -> None:
    pattern = re.compile(r"\b(local|lab|cloud)\b", re.IGNORECASE)
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(NODE_DIR.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


# AC6: the node's own stub scenario, dependencies BUS and CLK -------------------------------


def test_own_stub_scenario_passes_under_the_baseline_and_one_other_profile() -> None:
    document = yaml.safe_load((FIXTURES / "overlay_alpha_beta.yaml").read_text())
    document["bindings"]["beta"]["BUS"] = "reference"
    result = HandlerParityRun().handle(_request(_overlay(document)))
    assert result.passed
    assert [row.key for row in result.runs[0].rows] == ["m1", "m2", "m3"]
    assert all(
        row.content.endswith(FIXED_NOW.isoformat())
        for run in result.runs
        for row in run.rows
    )


def test_own_stub_scenario_fails_when_one_stub_drops_a_row() -> None:
    document = yaml.safe_load((FIXTURES / "overlay_alpha_beta.yaml").read_text())
    document["bindings"]["beta"]["BUS"] = "drops_row"
    result = HandlerParityRun().handle(
        _request(_overlay(document), messages=["m1", "m2", "m3", "m4"])
    )
    assert not result.passed
    assert result.first_difference.startswith("beta: verdict at index 0")


@pytest.mark.parametrize(
    ("bus_binding", "expected"),
    [("out_of_order", "row order"), ("duplicate", "count delivered")],
)
def test_own_stub_scenario_fails_on_reorder_and_duplicate_stubs(
    bus_binding: str, expected: str
) -> None:
    document = yaml.safe_load((FIXTURES / "overlay_alpha_beta.yaml").read_text())
    document["bindings"]["beta"]["BUS"] = bus_binding
    result = HandlerParityRun().handle(_request(_overlay(document)))
    assert not result.passed


def test_a_clock_that_goes_backward_fails_the_run() -> None:
    document = yaml.safe_load((FIXTURES / "overlay_alpha_beta.yaml").read_text())
    document["bindings"]["beta"]["CLK"] = "backward"
    result = HandlerParityRun().handle(_request(_overlay(document)))
    assert not result.passed
    assert "row content" in result.first_difference


# run_local: the local entry on the in-memory bus --------------------------------------------


def _wip_request() -> dict[str, JsonValue]:
    from omnimarket.handlers.rules_wip_gate import load_wip_gate_contract

    now = "2026-10-27T12:00:00Z"
    prs = {
        f"example#{i + 1}": {
            "facts": {
                "repo": "example",
                "number": i + 1,
                "state": "OPEN",
                "draft": False,
            }
        }
        for i in range(5)
    }
    return {
        "repo": "example",
        "dispatch_id": "dispatch-1",
        "now": now,
        "state": {"last_tick": now, "prs": prs},
        "policy": {
            "enforce_ruling": "enforcing",
            "dispatchable_repos": ["example"],
            "exempt_roles": [],
            "limits": {
                "example": {
                    "limit": 5,
                    "merges_per_hour": 1.5,
                    "method": "supplied measurement",
                    "evidence": "supplied evidence",
                }
            },
        },
        "config": load_wip_gate_contract()[1].model_dump(),
    }


def _run_local_config() -> ModelRunLocalConfig:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    return ModelRunLocalConfig.model_validate(contract["config"]["run_local"])


def test_run_local_runs_an_arbitrary_node_operation_on_the_in_memory_bus() -> None:
    import asyncio

    result = asyncio.run(
        run_local(
            "node_wip_gate_reducer",
            "wip_gate_decide",
            _wip_request(),
            _run_local_config(),
        )
    )
    assert result["outcome"] == "refuse"
    assert result["reason"] == "at_limit"


def test_run_local_refuses_an_operation_the_contract_does_not_route() -> None:
    import asyncio

    with pytest.raises(ValueError, match="no handler is routed"):
        asyncio.run(run_local("node_wip_gate_reducer", "nope", {}, _run_local_config()))


def test_node_operation_scenario_is_the_same_under_two_profiles() -> None:
    request = ModelParityRequest(
        scenario=ModelParityScenario(
            kind="node_operation",
            node="node_wip_gate_reducer",
            operation="wip_gate_decide",
            fixture={"request": _wip_request()},
        ),
        overlay=_fixture_overlay(),
        fixed_now=FIXED_NOW,
    )
    result = HandlerParityRun().handle(request)
    assert result.passed, result.first_difference
    assert {row.key for row in result.runs[0].rows} >= {"outcome", "reason"}


async def test_parity_run_works_inside_a_running_event_loop() -> None:
    result = HandlerParityRun().handle(_request(_fixture_overlay()))
    assert result.passed


def test_run_local_surfaces_a_handler_failure_to_the_caller() -> None:
    import asyncio

    with pytest.raises(ValidationError):
        asyncio.run(
            run_local(
                "node_wip_gate_reducer",
                "wip_gate_decide",
                {"repo": 1},
                _run_local_config(),
            )
        )


def test_contract_declares_the_command_and_completed_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.binding-parity-run-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.binding-parity-run-completed.v1"
    ]
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.evt.omnimarket.binding-parity-run-completed.v1",
        "failure": "onex.evt.omnimarket.binding-parity-run-failed.v1",
    }
    assert [d["name"] for d in contract["protocol_dependencies"]] == [
        "ProtocolEventBus",
        "ProtocolClock",
    ]
