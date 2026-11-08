# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract routes the reading, closed groups read WORKING and every named mutation turns the chain red (OMN-22947)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    ModelCanaryRun,
    ModelRunnerGroupReadingResult,
)
from tests.nodes.node_runner_group_reading_compute.support import (
    CLOSED_GROUP_ID,
    LAB_GROUP_ID,
    NAME,
    canary,
    closed_groups,
    lab_group,
    request,
)

#: The named mutations, each with the test that shows the golden chain red under it.
GOLDEN_MUTATIONS: dict[str, str] = {
    "group-setting-ignores-public": "test_golden_mutation_group_setting_ignores_public_turns_red",
    "public-never-sees-lab": "test_golden_mutation_public_never_sees_lab_turns_red",
    "canary-start-ignored": "test_golden_mutation_canary_start_ignored_turns_red",
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


def _opened_and_canary_started() -> list[Any]:
    """The three planted faults, as three requests."""
    open_group = (lab_group(group_id=LAB_GROUP_ID, allows_public=True),)
    started = canary(
        ModelCanaryRun(
            run_id=1, repository="example-public", picked_up_by_group_id=LAB_GROUP_ID
        )
    )
    return [request(open_group), request(closed_groups(), canary_facts=started)]


def assert_golden_chain(handler: Any) -> None:
    """Closed groups and a quiet canary read WORKING; each planted fault reads NOT WORKING and is named."""
    ok = handler.handle(request(closed_groups()))
    assert ok.verdict is EnumReadingVerdict.WORKING
    assert ok.failing_group_ids == ()

    opened, started = _opened_and_canary_started()
    flipped = handler.handle(opened)
    assert flipped.verdict is EnumReadingVerdict.NOT_WORKING
    assert flipped.failing_group_ids == (LAB_GROUP_ID,)
    by_name = {s.name: s for s in flipped.subjects}
    assert (
        by_name[f"group_setting:{LAB_GROUP_ID}"].verdict
        is EnumReadingVerdict.NOT_WORKING
    )
    assert (
        by_name[f"public_sees_lab:{LAB_GROUP_ID}"].verdict
        is EnumReadingVerdict.NOT_WORKING
    )

    canary_started = handler.handle(started)
    assert canary_started.verdict is EnumReadingVerdict.NOT_WORKING
    subject = {s.name: s for s in canary_started.subjects}["canary_public_workflow"]
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert subject.count == 1


class _GroupSettingIgnoresPublic(HandlerRunnerGroupReading):
    @staticmethod
    def _opens_public(*_args: Any) -> bool:
        return False


class _PublicNeverSeesLab(HandlerRunnerGroupReading):
    @staticmethod
    def _public_can_see(*_args: Any) -> list[str]:
        return []


class _CanaryStartIgnored(HandlerRunnerGroupReading):
    @staticmethod
    def _canary_starts(*_args: Any) -> list[Any]:
        return []


class _VerdictAlwaysWorking(HandlerRunnerGroupReading):
    def handle(self, request: Any) -> ModelRunnerGroupReadingResult:
        real = super().handle(request)
        return real.model_copy(update={"verdict": EnumReadingVerdict.WORKING})


def test_golden_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()

    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == contract["event_bus"]["subscribe_topics"][0]
        == "onex.cmd.omnimarket.runner-group-reading-requested.v1"
    )
    assert (
        contract["terminal_event"]
        == contract["event_bus"]["publish_topics"][0]
        == "onex.evt.omnimarket.runner-group-reading-computed.v1"
    )


def test_golden_chain_closed_groups_read_working_and_each_fault_reads_not_working() -> (
    None
):
    assert_golden_chain(_handler())


def test_golden_chain_the_hosted_group_that_allows_public_is_not_a_fault() -> None:
    result = _handler().handle(request(closed_groups()))
    by_name = {s.name: s for s in result.subjects}

    assert by_name["group_setting:9"].verdict is EnumReadingVerdict.WORKING
    assert (
        by_name[f"group_setting:{CLOSED_GROUP_ID}"].verdict
        is EnumReadingVerdict.WORKING
    )


def test_golden_mutation_group_setting_ignores_public_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_GroupSettingIgnoresPublic())


def test_golden_mutation_public_never_sees_lab_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_PublicNeverSeesLab())


def test_golden_mutation_canary_start_ignored_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_CanaryStartIgnored())


def test_golden_mutation_verdict_always_working_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_VerdictAlwaysWorking())


def test_golden_every_named_mutation_has_its_test() -> None:
    names = {n for n in globals() if n.startswith("test_") and callable(globals()[n])}

    assert set(GOLDEN_MUTATIONS.values()) <= names
