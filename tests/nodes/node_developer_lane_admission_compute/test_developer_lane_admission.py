# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A developer's selected lane and standing on it come from the binding and the lane's own list (OMN-21599).

The lane names below are fixture names; the node names no lane, so a test that renamed them all
would pass unchanged.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_developer_lane_admission_compute.handlers import (
    handler_developer_lane_admission as handler,
)
from omnimarket.nodes.node_developer_lane_admission_compute.handlers.handler_developer_lane_admission import (
    HandlerDeveloperLaneAdmission,
    admit,
)
from omnimarket.nodes.node_developer_lane_admission_compute.models import (
    ModelDeveloperLaneAdmissionRequest,
    ModelDeveloperLaneAdmissionResult,
)

pytestmark = pytest.mark.unit

NODE = Path(handler.__file__).resolve().parent.parent
HOME_LANE = "lane-home"
COLLAB_LANE = "lane-collab"
OTHER_LANE = "lane-other"
OPERATOR = "operator-principal"
COLLAB = "collab-principal-collab"
HOME_COLLAB = "collab-principal-home"


def declaration(*principals: str) -> dict[str, Any]:
    return {
        "schema_version": "developer_principal.v1",
        "grants_source": "none: test",
        "principals": [
            {"principal": name, "login": "x@example.invalid", "device": "d"}
            for name in principals
        ],
    }


def store(*, bound: str | None = None, **principals: str) -> dict[str, Any]:
    document: dict[str, Any] = {
        "lanes": {
            lane.replace("_", "-"): {"sasl_username": name}
            for lane, name in principals.items()
        }
    }
    if bound is not None:
        document["developer"] = {"lane_binding": bound}
    return document


def request(
    lane_store: dict[str, Any] | None,
    *,
    home_list: tuple[str, ...] | None = (HOME_COLLAB,),
    collab_list: tuple[str, ...] | None = (COLLAB,),
    barred: tuple[str, ...] = (HOME_LANE,),
) -> dict[str, Any]:
    return {
        "declared_lane": HOME_LANE,
        "barred_lanes": list(barred),
        "lane_store": lane_store,
        "lanes": {
            HOME_LANE: {
                "credential_source": "onex_lane_store",
                "declaration": None if home_list is None else declaration(*home_list),
            },
            COLLAB_LANE: {
                "credential_source": None,
                "declaration": None
                if collab_list is None
                else declaration(*collab_list),
            },
            OTHER_LANE: {"credential_source": None, "declaration": None},
        },
    }


def test_bound_collaborator_with_a_declared_principal_is_admitted() -> None:
    decision = admit(request(store(bound=COLLAB_LANE, lane_collab=COLLAB)))
    assert decision["selected_lane"] == COLLAB_LANE
    assert decision["verdict"] == "collaborator"
    assert decision["refusal"] is None


def test_binding_removed_is_refused() -> None:
    decision = admit(request(store(lane_collab=COLLAB)))
    assert decision["selected_lane"] == HOME_LANE
    assert (decision["verdict"], decision["refusal"]) == (
        "refused",
        "no-declared-principal",
    )
    assert decision["fault"] == "credential"


def test_principal_removed_is_refused() -> None:
    decision = admit(request(store(bound=COLLAB_LANE)))
    assert (decision["verdict"], decision["refusal"]) == (
        "refused",
        "no-declared-principal",
    )


def test_principal_stored_only_on_the_home_lane_does_not_stand_in_for_the_bound_lane() -> (
    None
):
    decision = admit(request(store(bound=COLLAB_LANE, lane_home=OPERATOR)))
    assert decision["refusal"] == "no-declared-principal"


def test_collaborator_selecting_the_barred_lane_is_refused() -> None:
    decision = admit(request(store(lane_home=HOME_COLLAB)))
    assert (decision["verdict"], decision["refusal"]) == (
        "refused",
        "collaborator-on-barred-lane",
    )


def test_collaborator_bound_explicitly_to_the_barred_lane_is_refused() -> None:
    decision = admit(request(store(bound=HOME_LANE, lane_home=HOME_COLLAB)))
    assert decision["refusal"] == "collaborator-on-barred-lane"


def machine_declaration(
    *, people: tuple[str, ...], machines: tuple[str, ...]
) -> dict[str, Any]:
    """The overlay shape of a lab deployment: people's devices, then machines it installs itself."""
    document = declaration(*people)
    document["principals"] += [
        {
            "principal": name,
            "login": "x@example.invalid",
            "device": "d",
            "role": "machine",
        }
        for name in machines
    ]
    return document


def machine_request(lane_store: dict[str, Any]) -> dict[str, Any]:
    built = request(lane_store)
    built["lanes"][HOME_LANE]["declaration"] = machine_declaration(
        people=(HOME_COLLAB,), machines=(OPERATOR,)
    )
    return built


def test_quiet_host_machine_row_on_the_barred_lane_is_the_operators_own_flow() -> None:
    """A lab host's login is declared with role machine; its hook events must keep flowing.

    Recorded: every hook event of a host whose login is a machine row of the overlay was dropped
    by the hook (the refusal below), so the host read as one that ran lanes and sent none.
    """
    decision = admit(machine_request(store(lane_home=OPERATOR)))
    assert (decision["selected_lane"], decision["verdict"]) == (HOME_LANE, "operator")
    assert decision["refusal"] is None


def test_quiet_host_no_false_alarm_machine_row_is_never_held_back_as_a_collaborator() -> (
    None
):
    decision = admit(machine_request(store(lane_home=OPERATOR)))
    # The hook holds back events only for this verdict, and drops all of them on a refusal.
    assert decision["verdict"] != handler.VERDICT_COLLABORATOR
    assert decision["refusal"] is None


def test_quiet_host_a_person_beside_machine_rows_is_still_refused_on_the_barred_lane() -> (
    None
):
    decision = admit(machine_request(store(lane_home=HOME_COLLAB)))
    assert decision["refusal"] == "collaborator-on-barred-lane"


def test_quiet_host_a_machine_row_is_not_a_person_of_another_lane() -> None:
    built = machine_request(store(bound=COLLAB_LANE, lane_collab=OPERATOR))
    decision = admit(built)
    assert decision["refusal"] is None
    assert decision["verdict"] == "operator"


def test_quiet_host_a_machine_row_without_a_principal_name_is_malformed() -> None:
    built = machine_request(store(lane_home=OPERATOR))
    built["lanes"][HOME_LANE]["declaration"]["principals"].append({"role": "machine"})
    assert admit(built)["refusal"] == "declaration-malformed"


def test_operators_own_home_lane_flow_is_unchanged() -> None:
    decision = admit(request(store(lane_home=OPERATOR)))
    assert (decision["selected_lane"], decision["verdict"]) == (HOME_LANE, "operator")
    assert decision["refusal"] is None


def test_operator_with_no_declaration_installed_is_unchanged() -> None:
    for lane_store in (None, {}, store(lane_home=OPERATOR)):
        decision = admit(request(lane_store, home_list=None, collab_list=None))
        assert decision["verdict"] == "operator"


def test_operator_with_no_lane_store_and_a_declaration_is_refused_for_no_principal() -> (
    None
):
    assert admit(request(None))["refusal"] == "no-declared-principal"


def test_bound_lane_with_no_declaration_installed_is_refused() -> None:
    decision = admit(
        request(store(bound=COLLAB_LANE, lane_collab=COLLAB), collab_list=None)
    )
    assert decision["refusal"] == "bound-lane-declaration-missing"


def test_principal_declared_on_another_lane_is_refused() -> None:
    decision = admit(request(store(bound=COLLAB_LANE, lane_collab=HOME_COLLAB)))
    assert decision["refusal"] == "principal-declared-for-another-lane"


def test_operator_principal_bound_to_a_collaborator_lane_stays_the_operator() -> None:
    decision = admit(request(store(bound=COLLAB_LANE, lane_collab=OPERATOR)))
    assert (decision["selected_lane"], decision["verdict"]) == (COLLAB_LANE, "operator")


@pytest.mark.parametrize("value", ["", "  ", 7, ["x"]])
def test_binding_that_is_not_a_lane_name_is_refused(value: object) -> None:
    decision = admit(request({"developer": {"lane_binding": value}}))
    assert decision["refusal"] == "binding-invalid"
    assert decision["fault"] == "config"


def test_developer_block_that_is_not_a_mapping_is_refused() -> None:
    assert admit(request({"developer": "x"}))["refusal"] == "binding-invalid"


def test_binding_to_an_unknown_lane_is_refused() -> None:
    decision = admit(request(store(bound="lane-nowhere", lane_collab=COLLAB)))
    assert decision["refusal"] == "binding-unknown-lane"
    assert decision["fault"] == "config"


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": "developer_principal.v1", "principals": []},
        {"schema_version": "other", "principals": [{"principal": "a"}]},
        {
            "schema_version": "developer_principal.v1",
            "principals": [{"principal": " "}],
        },
        {"schema_version": "developer_principal.v1", "principals": ["a"]},
    ],
)
def test_malformed_declaration_of_the_selected_lane_is_refused(
    bad: dict[str, Any],
) -> None:
    req = request(store(lane_home=OPERATOR))
    req["lanes"][HOME_LANE]["declaration"] = bad
    decision = admit(req)
    assert (decision["refusal"], decision["fault"]) == (
        "declaration-malformed",
        "config",
    )


def test_malformed_declaration_of_another_lane_is_not_the_selected_lanes_fault() -> (
    None
):
    req = request(store(bound=COLLAB_LANE, lane_collab=COLLAB))
    req["lanes"][HOME_LANE]["declaration"] = {"principals": []}
    assert admit(req)["verdict"] == "collaborator"


def test_operator_env_lane_reads_the_env_principal() -> None:
    req = request(None, home_list=(HOME_COLLAB,))
    req["lanes"][HOME_LANE].update(credential_source="operator_env_file")
    for env, verdict in ((OPERATOR, "operator"), (None, "refused")):
        req["lanes"][HOME_LANE]["env_principal"] = env
        assert admit(req)["verdict"] == verdict
    req["lanes"][HOME_LANE]["env_principal"] = HOME_COLLAB
    assert admit(req)["refusal"] == "collaborator-on-barred-lane"


def test_declared_lane_with_no_bus_identity_keeps_the_operator_behaviour() -> None:
    req = request(None)
    req["lanes"][HOME_LANE]["credential_source"] = None
    assert admit(req)["verdict"] == "operator"


def test_lane_store_principal_is_stripped_like_the_broker_identity() -> None:
    decision = admit(request(store(bound=COLLAB_LANE, lane_collab=f"  {COLLAB}  ")))
    assert decision["verdict"] == "collaborator"


def test_a_collaborator_lane_that_is_not_barred_admits_its_people() -> None:
    req = request(store(lane_home=HOME_COLLAB), barred=())
    assert admit(req)["verdict"] == "collaborator"


def test_runtime_entry_returns_the_typed_decision() -> None:
    typed = ModelDeveloperLaneAdmissionRequest(
        **request(store(bound=COLLAB_LANE, lane_collab=COLLAB))
    )
    result = HandlerDeveloperLaneAdmission().handle(typed)
    assert isinstance(result, ModelDeveloperLaneAdmissionResult)
    assert (result.selected_lane, result.verdict) == (COLLAB_LANE, "collaborator")


def test_vocabulary_of_the_handler_the_models_and_the_contract_agree() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["handler"]["class"] == "HandlerDeveloperLaneAdmission"
    refusals = {v for k, v in vars(handler).items() if k.startswith("REFUSAL_")}
    declared = set(
        ModelDeveloperLaneAdmissionResult.model_fields["refusal"]
        .annotation.__args__[0]
        .__args__
    )
    assert refusals == declared
    assert set(handler._REFUSAL_FAULT) == refusals


def test_handler_module_level_imports_the_standard_library_only() -> None:
    tree = ast.parse(Path(handler.__file__).read_text())
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "collections", "typing"}


def test_the_hook_can_load_the_handler_by_path() -> None:
    spec = importlib.util.spec_from_file_location("by_path", handler.__file__)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.admit(request(store(lane_home=OPERATOR)))["verdict"] == "operator"


def test_contract_publishes_the_decision_event() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    decided = "onex.evt.omnimarket.developer-lane-admission-decided.v1"
    assert contract["terminal_event"] == decided
    assert decided in contract["event_bus"]["publish_topics"]
