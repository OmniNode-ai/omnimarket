# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan first-slice compute: whether a plan names its first slice section.

The golden cases read the heading the check matches (a numbered one, a deeper one
under ``## Plan``), the words it counts and where the section ends; the error cases
pin the refusals (no matching heading, a section under the word minimum, a heading
inside a fence, a heading that merely carries the entry). The settings come from the
request, the handler's injected settings or the overlay, in that order.

Three injected breaks show the assertions can fail for real: a judge that matches any
heading carrying the word slice, a judge that reads a heading inside a fence, and a
judge that counts nothing and calls every plan ok.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.nodes.node_plan_first_slice_compute import HandlerPlanFirstSlice
from omnimarket.nodes.node_plan_first_slice_compute.handlers import (
    handler_plan_first_slice,
)
from omnimarket.nodes.node_plan_first_slice_compute.models.model_plan_first_slice import (
    EnumPlanFirstSliceVerdict,
    ModelPlanFirstSliceRequest,
    ModelPlanFirstSliceResult,
    ModelPlanFirstSliceSettings,
)
from omnimarket.nodes.node_plan_first_slice_compute.overlay import (
    NODE_NAME,
    load_settings,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NODE_DIR = Path(handler_plan_first_slice.__file__).resolve().parents[1]

SETTINGS = ModelPlanFirstSliceSettings(
    first_slice_headings=("first slice",),
    min_section_words=20,
    later_rung_headings=("later rungs", "ladder"),
)
OTHER_SETTINGS = ModelPlanFirstSliceSettings(
    first_slice_headings=("opening cut",),
    min_section_words=2,
    later_rung_headings=(),
)


def _words(count: int) -> str:
    return " ".join(["step"] * count)


GOLDEN_PLAN = f"## First slice\n\n{_words(25)}\n\n## Later rungs\n\n{_words(30)}\n"
NUMBERED_PLAN = f"## 3. First slice (the smallest cut)\n\n{_words(25)}\n"
DEEPER_PLAN = (
    "## Plan\n"
    "\n"
    f"{_words(5)}\n"
    "\n"
    "### First slice\n"
    "\n"
    f"{_words(25)}\n"
    "\n"
    "### Next rung of the plan\n"
    "\n"
    f"{_words(30)}\n"
)
NO_SECTION_PLAN = f"## Plan\n\n{_words(25)}\n"
SHORT_PLAN = f"## First slice\n\n{_words(5)}\n"
FENCED_PLAN = f"## Plan\n\n```text\n## First slice\n\n{_words(25)}\n```\n"
DEEPER_RUNG_PLAN = (
    f"## First slice\n\n{_words(25)}\n\n#### Later rungs\n\n{_words(30)}\n"
)
OPENING_CUT_PLAN = "## Opening cut\n\nstep step\n"
OPENING_SLICE_PLAN = f"## Opening slice\n\n{_words(4)}\n"


def _request(
    plan_text: str, settings: ModelPlanFirstSliceSettings | None = SETTINGS
) -> ModelPlanFirstSliceRequest:
    return ModelPlanFirstSliceRequest(plan_text=plan_text, settings=settings)


def _expect(
    plan_text: str, verdict: EnumPlanFirstSliceVerdict
) -> ModelPlanFirstSliceResult:
    result = HandlerPlanFirstSlice().handle(_request(plan_text))
    assert result.verdict is verdict, result.detail
    return result


# ---------------------------------------------------------------------------------------------- golden


def test_golden_the_first_slice_section_is_matched_and_counted() -> None:
    result = _expect(GOLDEN_PLAN, EnumPlanFirstSliceVerdict.OK)
    assert result.heading == "First slice"
    assert result.section_words == 25, "words after ## Later rungs are not counted"


def test_golden_a_numbered_heading_matches() -> None:
    result = _expect(NUMBERED_PLAN, EnumPlanFirstSliceVerdict.OK)
    assert result.heading == "3. First slice (the smallest cut)"
    assert result.section_words == 25


def test_golden_a_deeper_heading_matches_and_ends_at_the_same_level() -> None:
    result = _expect(DEEPER_PLAN, EnumPlanFirstSliceVerdict.OK)
    assert result.heading == "First slice"
    assert result.section_words == 25, "the words around the section are not counted"


def test_golden_the_request_settings_win_over_the_injected_ones() -> None:
    handler = HandlerPlanFirstSlice(SETTINGS)
    result = handler.handle(_request(OPENING_CUT_PLAN, settings=OTHER_SETTINGS))
    assert result.verdict is EnumPlanFirstSliceVerdict.OK
    assert result.heading == "Opening cut"
    assert result.section_words == 2
    assert (
        handler.handle(_request(OPENING_CUT_PLAN)).verdict
        is EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION
    ), "under the injected settings the same plan names no first slice section"


# ----------------------------------------------------------------------------------------------- error


def test_error_no_heading_names_a_first_slice_section() -> None:
    result = _expect(NO_SECTION_PLAN, EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)
    assert result.heading is None
    assert result.section_words == 0


def test_error_a_first_slice_under_the_word_minimum_is_empty() -> None:
    result = _expect(SHORT_PLAN, EnumPlanFirstSliceVerdict.EMPTY_FIRST_SLICE_SECTION)
    assert result.heading == "First slice"
    assert result.section_words == 5


def test_error_a_first_slice_inside_a_fence_is_not_plan_structure() -> None:
    result = _expect(FENCED_PLAN, EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)
    assert result.heading is None


def test_error_a_heading_that_merely_carries_the_entry_does_not_match() -> None:
    plan = f"## Not the first slice\n\n{_words(25)}\n"
    result = _expect(plan, EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)
    assert result.heading is None


def test_error_an_empty_plan_names_no_first_slice_section() -> None:
    result = _expect("", EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)
    assert result.heading is None
    assert result.section_words == 0


def test_error_a_later_rung_heading_deeper_than_the_section_still_ends_it() -> None:
    result = _expect(DEEPER_RUNG_PLAN, EnumPlanFirstSliceVerdict.OK)
    assert result.heading == "First slice"
    assert result.section_words == 25, "#### Later rungs ends it despite ####"


# ------------------------------------------------------------------------------------------------ chain


def test_chain_the_golden_check_emits_a_start_then_a_success() -> None:
    sink = InMemoryEmissionSink()
    result = _expect(GOLDEN_PLAN, EnumPlanFirstSliceVerdict.OK)
    chain = ChainEmitter(
        sink,
        node="node_plan_first_slice_compute",
        piece=9,
        correlation_id="plan-golden",
        backend="in_memory",
    )
    chain.start()
    chain.success({"plans_ok": 1})
    assert [e.correlation_id for e in sink.events] == ["plan-golden", "plan-golden"]
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert sink.events[1].counters == {"plans_ok": 1}
    assert sink.events[1].failure_code is None
    assert result.verdict is EnumPlanFirstSliceVerdict.OK


def test_chain_the_error_check_emits_a_start_then_a_typed_failure() -> None:
    sink = InMemoryEmissionSink()
    result = _expect(NO_SECTION_PLAN, EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)
    chain = ChainEmitter(
        sink,
        node="node_plan_first_slice_compute",
        piece=9,
        correlation_id="plan-error",
        backend="in_memory",
    )
    chain.start()
    chain.failure(result.verdict.value)
    assert [e.correlation_id for e in sink.events] == ["plan-error", "plan-error"]
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == "no_first_slice_section"


# --------------------------------------------------------------------------------------------- settings


def test_settings_with_no_settings_in_the_request_the_overlay_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {
            "first_slice_headings": ["opening slice"],
            "min_section_words": 3,
            "later_rung_headings": [],
        },
    )
    assert load_settings() == ModelPlanFirstSliceSettings(
        first_slice_headings=("opening slice",),
        min_section_words=3,
        later_rung_headings=(),
    )
    result = HandlerPlanFirstSlice().handle(
        ModelPlanFirstSliceRequest(plan_text=OPENING_SLICE_PLAN)
    )
    assert result.verdict is EnumPlanFirstSliceVerdict.OK
    assert result.heading == "Opening slice"
    assert result.section_words == 4


def test_settings_with_no_overlay_the_packaged_contract_config_is_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert load_settings() == ModelPlanFirstSliceSettings(
        first_slice_headings=("first slice",),
        min_section_words=20,
        later_rung_headings=("later rungs", "ladder"),
    )
    result = HandlerPlanFirstSlice().handle(
        ModelPlanFirstSliceRequest(plan_text=GOLDEN_PLAN)
    )
    assert result.verdict is EnumPlanFirstSliceVerdict.OK
    assert result.section_words == 25


# ---------------------------------------------------------------------------------------------- contract


def test_contract_declares_the_command_topic_and_its_handler() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_plan_first_slice_compute"
    for side, model in (
        ("input_model", ModelPlanFirstSliceRequest),
        ("output_model", ModelPlanFirstSliceResult),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerPlanFirstSlice
    )
    dispatch = contract["runtime_dispatch"]
    assert (
        dispatch["command_topic"]
        == "onex.cmd.omnimarket.plan-first-slice-check-requested.v1"
    )


def test_contract_subscribes_the_runtime_to_the_command_topic() -> None:
    # The dev runtime subscribes only to event_bus.subscribe_topics, so a node that
    # declares runtime_dispatch.command_topic alone never receives its command.
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    dispatch = contract["runtime_dispatch"]
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [dispatch["command_topic"]]
    assert set(bus["publish_topics"]) == set(dispatch["terminal_events"].values())
    assert contract["runtime_profiles"] == ["main"]
    granted = {
        (row["name"], row["operation"])
        for block in contract["broker_acl_grants"]
        for row in block["grants"]
    }
    assert granted == {
        (dispatch["command_topic"], "READ"),
        (dispatch["command_topic"], "DESCRIBE"),
        *(
            (topic, op)
            for topic in bus["publish_topics"]
            for op in ("WRITE", "DESCRIBE")
        ),
    }


def test_handler_does_no_io() -> None:
    tree = ast.parse(Path(handler_plan_first_slice.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "subprocess", "socket", "time", "random", "pathlib"}


# ------------------------------------------------------------------------------------------- determinism


def test_the_same_request_judged_twice_gives_the_same_result() -> None:
    handler = HandlerPlanFirstSlice(SETTINGS)
    request = _request(GOLDEN_PLAN)
    assert handler.handle(request) == handler.handle(request)
    empty = ModelPlanFirstSliceRequest(plan_text="", settings=SETTINGS)
    assert handler.handle(empty) == handler.handle(empty)
    assert request.model_dump_json() == _request(GOLDEN_PLAN).model_dump_json()


# ---------------------------------------------------------------------------------------- injected breaks


def test_break_any_heading_containing_slice_turns_the_no_section_assertion_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = f"## Not the first slice\n\n{_words(25)}\n"
    # the real judge holds it
    _expect(plan, EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)

    real = handler_plan_first_slice.check_plan_first_slice

    def matches_any_slice_heading(
        request: ModelPlanFirstSliceRequest,
        settings: ModelPlanFirstSliceSettings,
    ) -> ModelPlanFirstSliceResult:
        for line in request.plan_text.splitlines():
            if line.startswith("#") and "slice" in line.lower():
                return ModelPlanFirstSliceResult(
                    verdict=EnumPlanFirstSliceVerdict.OK,
                    heading=line.lstrip("#").strip(),
                    section_words=99,
                    detail="the break: any heading with slice is a first slice",
                )
        return real(request, settings)

    monkeypatch.setattr(
        handler_plan_first_slice, "check_plan_first_slice", matches_any_slice_heading
    )
    with pytest.raises(AssertionError):
        _expect(plan, EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)


def test_break_ignores_fences_turns_the_fenced_assertion_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the real judge holds it
    _expect(FENCED_PLAN, EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)

    real = handler_plan_first_slice.check_plan_first_slice

    def reads_inside_fences(
        request: ModelPlanFirstSliceRequest,
        settings: ModelPlanFirstSliceSettings,
    ) -> ModelPlanFirstSliceResult:
        unwrapped = "\n".join(
            line
            for line in request.plan_text.splitlines()
            if not line.lstrip().startswith("```")
        )
        return real(request.model_copy(update={"plan_text": unwrapped}), settings)

    monkeypatch.setattr(
        handler_plan_first_slice, "check_plan_first_slice", reads_inside_fences
    )
    with pytest.raises(AssertionError):
        _expect(FENCED_PLAN, EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)


def test_break_counts_nothing_turns_the_empty_plan_assertion_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the real judge holds it
    _expect("", EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)

    def always_ok(
        request: ModelPlanFirstSliceRequest,
        settings: ModelPlanFirstSliceSettings,
    ) -> ModelPlanFirstSliceResult:
        return ModelPlanFirstSliceResult(
            verdict=EnumPlanFirstSliceVerdict.OK,
            heading="First slice",
            section_words=99,
            detail="the break: every plan is ok",
        )

    monkeypatch.setattr(handler_plan_first_slice, "check_plan_first_slice", always_ok)
    with pytest.raises(AssertionError):
        _expect("", EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION)
