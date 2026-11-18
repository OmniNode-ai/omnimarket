# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A command on the ask done check's request topic reaches a handler the runtime can build (OMN-24993).

The contract maps the command topic to a subscription and the two result topics to publications, with
the broker grants they need. The handler the contract names is built with no arguments, as the runtime
builds it: with no overlay, or no floor URL, it answers every ask UNKNOWN and appends nothing; with a
deployment it hands the request to the checking handler.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_ask_done_check_effect.adapters import InMemoryFloorRead
from omnimarket.nodes.node_ask_done_check_effect.handlers import (
    HandlerAskDoneCheck,
    HandlerAskDoneCheckBus,
)
from omnimarket.nodes.node_ask_done_check_effect.models import (
    EnumCheckVerdict,
    ModelAskDoneCheckRequest,
    ModelAskToCheck,
)
from omnimarket.nodes.node_ask_done_check_effect.protocols import ModelFloorRead
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_ask_done_check_effect"
)
CONTRACT = NODE_DIR / "contract.yaml"
EXAMPLE = NODE_DIR / "overlay.example.yaml"
SAID = datetime(2026, 11, 6, 14, 27, 51, tzinfo=UTC)
NOW = datetime(2026, 11, 6, 17, 30, tzinfo=UTC)
ASK = "ask-00000000a1"
ROW_CHECK = "row:lane=host-reading,probe=ledger-durability,host=h053,verdict=WORKING"
PROBE_ROW = (
    "2026-11-06T17:22:47Z | STATUS | lane=host-reading | src=host-reading | host=h053 | "
    "probe=ledger-durability | verdict=WORKING | drill=no | durable_lag_s=28"
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _request(check: str = ROW_CHECK, write: bool = True) -> ModelAskDoneCheckRequest:
    return ModelAskDoneCheckRequest(
        asks=(ModelAskToCheck(ask_id=ASK, said_at=SAID, done_check=check),), write=write
    )


def test_event_bus_subscribes_the_command_topic_and_publishes_both_result_events() -> (
    None
):
    contract = _contract()
    dispatch = contract["runtime_dispatch"]
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [dispatch["command_topic"]]
    assert bus["publish_topics"] == [
        dispatch["terminal_events"]["success"],
        dispatch["terminal_events"]["failure"],
    ]
    assert contract["terminal_event"] == dispatch["terminal_events"]["success"]
    assert (
        dispatch["command_topic"] == "onex.cmd.omnimarket.ask-done-check-requested.v1"
    )


def test_every_declared_topic_has_the_broker_grant_its_direction_needs() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    grants = contract["broker_acl_grants"]
    assert len(grants) == 1
    seen = {(g["name"], g["operation"]) for g in grants[0]["grants"]}
    for topic in bus["subscribe_topics"]:
        assert {(topic, "READ"), (topic, "DESCRIBE")} <= seen
    for topic in bus["publish_topics"]:
        assert {(topic, "WRITE"), (topic, "DESCRIBE")} <= seen
    assert len(seen) == len(grants[0]["grants"])


def test_result_events_are_declared_as_externally_consumed() -> None:
    contract = _contract()
    assert set(contract["externally_consumed_topics"]) == set(
        contract["event_bus"]["publish_topics"]
    )


def test_the_contract_names_a_handler_the_runtime_can_build_with_no_arguments() -> None:
    contract = _contract()
    [route] = contract["handler_routing"]["handlers"]
    assert route["topic"] == contract["event_bus"]["subscribe_topics"][0]
    assert route["handler"]["name"] == contract["handler"]["class"]
    assert route["handler"]["module"] == contract["handler"]["module"]
    module = importlib.import_module(contract["handler"]["module"])
    built = getattr(module, contract["handler"]["class"])()
    assert isinstance(built, HandlerAskDoneCheckBus)
    # The checking handler needs constructor parameters, so it is bound as a secondary handler only.
    secondary = contract["secondary_handlers"]["ask_done_check"]["handler"]
    assert secondary["name"] == HandlerAskDoneCheck.__name__
    assert contract["descriptor"]["runtime_profiles"] == ["effects"]


def test_no_overlay_reads_every_ask_unknown_and_appends_nothing() -> None:
    [verdict] = HandlerAskDoneCheckBus(environ={}).handle(_request()).verdicts
    assert verdict.verdict is EnumCheckVerdict.UNKNOWN
    assert verdict.reason.startswith("deployment-unreadable:")
    assert verdict.row == ""
    assert verdict.written == "no"
    assert verdict.ask_id == ASK


def test_an_overlay_whose_floor_url_is_not_set_reads_unknown_and_names_the_variable() -> (
    None
):
    env = {"ONEX_ASK_DONE_CHECK_OVERLAY": str(EXAMPLE)}
    [verdict] = HandlerAskDoneCheckBus(environ=env).handle(_request()).verdicts
    assert verdict.verdict is EnumCheckVerdict.UNKNOWN
    assert "ONEX_FLOOR_JSON_URL" in verdict.reason
    assert verdict.written == "no"


def test_a_deployment_hands_the_request_to_the_checking_handler() -> None:
    ledger = InMemoryLedger([PROBE_ROW])
    env = {"ONEX_ASK_DONE_CHECK_OVERLAY": str(EXAMPLE)}

    def factory(overlay: Any, _env: Any) -> HandlerAskDoneCheck:
        floor = InMemoryFloorRead(
            ModelFloorRead(pieces={}, generated_at="2026-11-06T17:30:00Z")
        )
        return HandlerAskDoneCheck(overlay, floor, ledger, ledger, clock=lambda: NOW)

    [verdict] = (
        HandlerAskDoneCheckBus(environ=env, factory=factory).handle(_request()).verdicts
    )
    assert verdict.verdict is EnumCheckVerdict.TRUE
    assert verdict.written == "yes"
    assert f"ask={ASK}" in ledger.lines[-1]
