# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21818 -- a bus reports its Empty consumer-group count and fails over the contract's threshold."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from omnimarket.models.consumer_group_guard import (
    ModelConsumerGroupCensus,
    ModelConsumerGroupGuardRequest,
)
from omnimarket.nodes.node_consumer_group_guard_effect import (
    HandlerConsumerGroupGuardEffect,
)
from omnimarket.nodes.node_consumer_group_guard_effect.__main__ import main
from omnimarket.nodes.node_consumer_group_guard_effect.handlers import (
    handler_consumer_group_guard as guard,
)

pytestmark = pytest.mark.unit

NODE = Path(guard.__file__).resolve().parents[1]
LIMIT = guard.contract_threshold()


def _broker(_lane: str) -> tuple[str, str, dict[str, str]]:
    return "dev-broker:19092", "local", {"bootstrap.servers": "dev-broker:19092"}


def _handler(counts: dict[str, int]) -> HandlerConsumerGroupGuardEffect:
    def read(_config: Mapping[str, str]) -> dict[str, int]:
        return counts

    return HandlerConsumerGroupGuardEffect(read, _broker)


def _run(counts: dict[str, int]) -> ModelConsumerGroupCensus:
    return asyncio.run(
        _handler(counts).handle(ModelConsumerGroupGuardRequest(bus_lane="dev"))
    )


def test_the_threshold_is_declared_in_the_contract_and_in_no_code_constant() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["consumer_group_guard"]["empty_group_max"] == LIMIT
    source = Path(guard.__file__).read_text(encoding="utf-8")
    assert str(LIMIT) not in source


def test_a_planted_census_over_the_threshold_fails() -> None:
    census = _run({"EMPTY": LIMIT + 1, "STABLE": 311})
    assert census.status == "FAIL"
    assert (census.groups_empty, census.groups_total) == (LIMIT + 1, LIMIT + 312)
    assert census.empty_group_max == LIMIT
    assert "over" in census.detail


def test_the_dev_brokers_measured_census_fails() -> None:
    # Read from the dev broker at 2026-10-31T18:09Z, three crashes in.
    assert _run({"EMPTY": 19154, "STABLE": 311}).status == "FAIL"


def test_a_census_at_the_threshold_passes() -> None:
    census = _run({"EMPTY": LIMIT, "STABLE": 311})
    assert census.status == "OK"
    assert census.groups_other == 311


def test_an_unreadable_census_fails_and_never_reads_as_zero_groups() -> None:
    def read(_config: Mapping[str, str]) -> dict[str, int]:
        raise RuntimeError("SASL password=hunter2 refused")

    census = asyncio.run(
        HandlerConsumerGroupGuardEffect(read, _broker).handle(
            ModelConsumerGroupGuardRequest(bus_lane="dev")
        )
    )
    assert census.status == "FAIL"
    assert census.groups_empty is None
    assert "hunter2" not in census.model_dump_json()
    assert census.error == "group census unreadable: RuntimeError"


def test_the_entry_point_exits_one_over_the_threshold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        guard,
        "read_group_states",
        lambda _config: {"EMPTY": LIMIT + 5},
    )
    monkeypatch.setattr(guard, "resolve_broker", _broker)
    assert main(["--bus-lane", "dev"]) == 1
    assert '"status": "FAIL"' in capsys.readouterr().out
