# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract routes the reading, a recorded floor reads WORKING and every named mutation turns the chain red (OMN-22963)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_dispatch_reading_compute.handlers import (
    HandlerDispatchReading,
)
from omnimarket.nodes.node_dispatch_reading_compute.models import (
    ModelDispatchReadingResult,
)
from omnimarket.nodes.node_dispatch_reading_compute.tests.support import (
    NAME,
    SCRATCH_SCRIPT,
    clean_lanes,
    lane,
    record,
    request,
)

#: The named mutations, each with the test that shows the golden chain red under it.
GOLDEN_MUTATIONS: dict[str, str] = {
    "scratch-source-ignored": "test_golden_mutation_scratch_source_ignored_turns_red",
    "unrecorded-lane-ignored": "test_golden_mutation_unrecorded_lane_ignored_turns_red",
    "missing-terminal-ignored": "test_golden_mutation_missing_terminal_ignored_turns_red",
    "late-terminal-ignored": "test_golden_mutation_late_terminal_ignored_turns_red",
    "verdict-always-working": "test_golden_mutation_verdict_always_working_turns_red",
}


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    return loaded


def _handler() -> Any:
    handler = _contract()["handler_routing"]["handlers"][0]["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])()


def assert_golden_chain(handler: Any) -> None:
    """A recorded floor reads WORKING; each planted fault reads NOT WORKING and is named."""
    ok = handler.handle(request())
    assert ok.verdict is EnumReadingVerdict.WORKING
    assert ok.lanes_without_record == ()
    assert ok.unreconciled == ()

    drill = lane("drill-1", kind="drill", launched_from=SCRATCH_SCRIPT)
    scratch = handler.handle(request((*clean_lanes(), drill)))
    assert scratch.verdict is EnumReadingVerdict.NOT_WORKING
    assert [(n.lane, n.from_scratch_script) for n in scratch.lanes_without_record] == [
        ("drill-1", True)
    ]

    missing = handler.handle(
        request(
            clean_lanes(),
            (
                record("fill-a"),
                record("fill-b", terminal_after=None, deadline_seconds=600),
            ),
        )
    )
    assert missing.verdict is EnumReadingVerdict.NOT_WORKING
    assert [u.dispatch_id for u in missing.unreconciled] == ["dispatch-fill-b"]

    late = handler.handle(
        request(
            clean_lanes(),
            (
                record("fill-a"),
                record("fill-b", terminal_after=900, deadline_seconds=600),
            ),
        )
    )
    assert late.verdict is EnumReadingVerdict.NOT_WORKING
    assert [u.dispatch_id for u in late.unreconciled] == ["dispatch-fill-b"]


class _ScratchSourceIgnored(HandlerDispatchReading):
    @staticmethod
    def _from_scratch(*_args: Any) -> bool:
        return False


class _UnrecordedLaneIgnored(HandlerDispatchReading):
    def _lanes_without_record(self, *_args: Any) -> tuple[()]:
        return ()


class _MissingTerminalIgnored(HandlerDispatchReading):
    def _unreconciled_reason(self, record: Any, now_past_deadline: bool) -> str | None:
        if record.terminal is None:
            return None
        return super()._unreconciled_reason(record, now_past_deadline)


class _LateTerminalIgnored(HandlerDispatchReading):
    def _unreconciled_reason(self, record: Any, now_past_deadline: bool) -> str | None:
        if record.terminal is not None:
            return None
        return super()._unreconciled_reason(record, now_past_deadline)


class _VerdictAlwaysWorking(HandlerDispatchReading):
    def handle(self, request: Any) -> ModelDispatchReadingResult:
        real = super().handle(request)
        return real.model_copy(update={"verdict": EnumReadingVerdict.WORKING})


def test_golden_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()

    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == contract["event_bus"]["subscribe_topics"][0]
        == "onex.cmd.omnimarket.dispatch-reading-requested.v1"
    )
    assert (
        contract["terminal_event"]
        == contract["event_bus"]["publish_topics"][0]
        == "onex.evt.omnimarket.dispatch-reading-computed.v1"
    )


def test_golden_chain_recorded_floor_reads_working_and_each_fault_reads_not_working() -> (
    None
):
    assert_golden_chain(_handler())


def test_golden_mutation_scratch_source_ignored_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_ScratchSourceIgnored())


def test_golden_mutation_unrecorded_lane_ignored_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_UnrecordedLaneIgnored())


def test_golden_mutation_missing_terminal_ignored_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_MissingTerminalIgnored())


def test_golden_mutation_late_terminal_ignored_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_LateTerminalIgnored())


def test_golden_mutation_verdict_always_working_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_VerdictAlwaysWorking())


def test_golden_chain_every_named_mutation_has_its_test() -> None:
    names = {n for n in globals() if n.startswith("test_") and callable(globals()[n])}

    assert set(GOLDEN_MUTATIONS.values()) <= names
