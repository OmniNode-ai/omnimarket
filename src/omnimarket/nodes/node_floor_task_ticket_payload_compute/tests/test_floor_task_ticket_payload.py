# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_task_ticket_payload_compute: the payload of one floor task's ticket, or the parts the plan lacks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_floor_task_ticket_payload_compute.handlers.handler_floor_task_ticket_payload import (
    HandlerFloorTaskTicketPayload,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.models import (
    ModelFloorTaskTicketPayload,
    ModelFloorTaskTicketPayloadResult,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.tests.support import (
    LINE,
    PARENT,
    PLAN,
    PLAN_WITHOUT_TICKET,
    request,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parent.parent


def run(*args: object, **kwargs: object) -> ModelFloorTaskTicketPayloadResult:
    return HandlerFloorTaskTicketPayload().handle(request(*args, **kwargs))  # type: ignore[arg-type]


def test_payload_carries_parent_title_key_and_verbatim_criteria() -> None:
    result = run("T1")
    assert result.verdict == "PAYLOAD"
    assert result.ok
    payload = result.payload
    assert payload is not None
    assert payload.parent == PARENT
    assert payload.floor_key == f"floor:{LINE}:T1"
    assert payload.title == "T1. The example node: `node_example_compute`"
    assert (
        "- [ ] AC1: The node returns the verdict for the fixture -- falsifier: `-k fixture_verdicts`."
        in payload.description
    )
    assert (
        "- [ ] AC2: The same input twice is byte-identical -- falsifier: run twice, compare `model_dump_json`."
        in payload.description
    )


def test_payload_description_has_gate_floor_key_and_definition_of_done() -> None:
    payload = run("T1").payload
    assert payload is not None
    lines = payload.description.splitlines()
    assert lines[0] == f"Gate: {PARENT} AC-1"
    assert f"Floor key: floor:{LINE}:T1" in lines
    assert sum(line.startswith("Floor key:") for line in lines) == 1
    assert "## Definition of done" in lines
    dod = payload.description.split("## Definition of done", 1)[1]
    assert "merged" in dod
    assert "dev lane" in dod
    assert (
        "Integration check: run the node on the lab host with the recorded fixture and read its exit code."
        in dod
    )
    assert dod.count("falsifier:") == 3


def test_floor_key_line_is_ahead_of_the_criteria_so_no_criterion_absorbs_it() -> None:
    payload = run("T1").payload
    assert payload is not None
    text = payload.description
    assert text.index("Floor key:") < text.index("## Acceptance criteria")


def test_a_wrapped_criterion_is_one_criterion_with_its_falsifier() -> None:
    payload = run("T6").payload
    assert payload is not None
    assert (
        "- [ ] AC1: A criterion that wraps onto a second line -- falsifier: `-k wrapped`."
        in payload.description
    )
    assert payload.description.count("- [ ] AC") == 2
    assert "this field is not read" not in payload.description


def test_a_lab_step_of_none_that_names_the_task_that_runs_it_is_an_integration_check() -> (
    None
):
    result = run("T5")
    assert result.verdict == "PAYLOAD"
    assert result.payload is not None
    assert (
        "Integration check: none here; T9 runs it against the live record."
        in result.payload.description
    )


def test_a_task_id_is_matched_whole_not_as_a_prefix() -> None:
    plan = PLAN.replace("### T1. ", "### T10. ").replace("### T2. ", "### T1. ")
    result = HandlerFloorTaskTicketPayload().handle(request("T1", plan))
    assert result.verdict == "NEEDS-SPEC"
    assert result.payload is None
    assert result.missing == ("falsifier:2",)


def test_a_falsifier_missing_names_the_criterion() -> None:
    result = run("T2")
    assert result.verdict == "NEEDS-SPEC"
    assert result.missing == ("falsifier:2",)
    assert result.cause == "needs-spec:falsifier:2"
    assert result.payload is None


def test_no_criteria_is_needs_spec_naming_criteria() -> None:
    result = run("T3")
    assert result.verdict == "NEEDS-SPEC"
    assert result.missing == ("criteria",)


def test_no_integration_check_is_needs_spec_naming_it() -> None:
    result = run("T4")
    assert result.verdict == "NEEDS-SPEC"
    assert result.missing == ("integration-check",)
    assert result.cause == "needs-spec:integration-check"


def test_every_missing_part_is_named_at_once() -> None:
    plan = PLAN.replace(
        "- **Lab step:** run it on the lab host.\n- **Acceptance criteria:**\n"
        "  1. The first criterion",
        "- **Acceptance criteria:**\n  1. The first criterion",
    )
    result = HandlerFloorTaskTicketPayload().handle(request("T2", plan))
    assert result.missing == ("falsifier:2", "integration-check")
    assert result.cause == "needs-spec:falsifier:2,integration-check"


def test_a_task_the_plan_has_no_section_for_is_needs_spec() -> None:
    result = run("T99")
    assert result.verdict == "NEEDS-SPEC"
    assert result.missing == ("task-section",)


def test_a_plan_header_with_no_ticket_is_no_parent_whatever_the_task_says() -> None:
    for task in ("T1", "T2", "T3"):
        result = run(task, PLAN_WITHOUT_TICKET)
        assert result.verdict == "NO-PARENT", task
        assert result.cause == "no-parent"
        assert result.payload is None


def test_a_ticket_named_after_the_header_is_not_the_parent() -> None:
    plan = PLAN_WITHOUT_TICKET.replace(
        "## 1. In plain words", "## 1. In plain words\n\n- **Ticket:** OMN-70009"
    )
    assert run("T1", plan).verdict == "NO-PARENT"


def test_an_unread_plan_is_plan_unread_and_never_an_empty_payload() -> None:
    result = run("T1", None)
    assert result.verdict == "PLAN-UNREAD"
    assert result.cause == "plan-unread"
    assert result.payload is None


def test_payload_has_no_source_fields() -> None:
    declared = set(ModelFloorTaskTicketPayload.model_fields)
    assert declared == {"floor_key", "title", "description", "parent"}
    for forbidden in ("project", "projectId", "label", "labels", "pillar"):
        assert forbidden not in declared
    payload = run("T1").payload
    assert payload is not None
    assert set(payload.model_dump()) == declared
    with pytest.raises(ValidationError):
        ModelFloorTaskTicketPayload(
            floor_key="k",
            title="t",
            description="d",
            parent=PARENT,
            project="p",  # type: ignore[call-arg]
        )


def test_result_shape_refuses_a_verdict_that_contradicts_its_fields() -> None:
    for bad in (
        {"verdict": "PAYLOAD"},
        {"verdict": "NEEDS-SPEC", "cause": "needs-spec:x"},
        {"verdict": "NEEDS-SPEC", "missing": ("x",), "cause": "wrong"},
        {"verdict": "NO-PARENT", "cause": "needs-spec:x"},
        {"verdict": "PLAN-UNREAD", "cause": ""},
        {"verdict": "NO-PARENT", "cause": "no-parent", "missing": ("x",)},
    ):
        with pytest.raises(ValidationError):
            ModelFloorTaskTicketPayloadResult(**bad)


def test_same_input_twice_is_byte_identical() -> None:
    assert run("T1").model_dump_json() == run("T1").model_dump_json()


def test_handler_reads_no_clock_and_does_no_io() -> None:
    source = (
        NODE_DIR / "handlers" / "handler_floor_task_ticket_payload.py"
    ).read_text()
    for forbidden in (
        "datetime",
        "time.time",
        "open(",
        "read_text",
        "requests",
    ):
        assert forbidden not in source, forbidden
    assert not re.search(
        r"^\s*(import|from)\s+(os|subprocess|socket|httpx|urllib)\b", source, re.M
    )


# ------------------------------------------------------------------ G-NODE: contract, keys, seams


def test_contract_loads_with_archetype_models_routing_and_descriptor() -> None:
    import importlib

    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_floor_task_ticket_payload_compute"
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["node_archetype"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    [route] = contract["handler_routing"]["handlers"]
    for dotted in (route["input_model"], route["output_model"]):
        module, _, name = dotted.rpartition(".")
        assert getattr(importlib.import_module(module), name)
    module = importlib.import_module(route["handler"]["module"])
    assert getattr(module, route["handler"]["name"]) is HandlerFloorTaskTicketPayload
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == contract["name"]
    assert metadata["capabilities"]["side_effect_class"] == "none"


def test_seams_in_spi_a_pure_compute_injects_nothing() -> None:
    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert (contract.get("protocol_dependencies") or []) == []
    HandlerFloorTaskTicketPayload()


def test_the_node_declares_no_overlay_key_because_it_reads_no_setting() -> None:
    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert "deployment_overlay" not in contract


def test_node_holds_no_ticket_id() -> None:
    for path in NODE_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        assert re.search(r"OMN-[0-9]+", path.read_text()) is None, path
