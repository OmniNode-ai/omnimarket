# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No routed entry returns a model the contract does not map, and the fallback topic is no other node's input."""

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml

from omnimarket.nodes.node_merge_override_alarm_effect.handlers.handler_merge_override_entries import (
    HandlerMergeOverridePrMerged,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    ModelMergeOverrideResult,
)
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    READ_SUFFIX,
    make_rig,
    pr_merged_event,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
MINE = "node_merge_override_alarm_effect"
READ_REQUEST = "onex.cmd.omnimarket.pr-landing-github-requested.v1"


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load((NODES / MINE / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def routed_returns(contract: dict[str, Any]) -> dict[str, Any]:
    """Each routed handler class name -> the return type of its handle()."""
    returns: dict[str, Any] = {}
    for entry in contract["handler_routing"]["handlers"]:
        handler = getattr(
            import_module(entry["handler"]["module"]), entry["handler"]["name"]
        )
        returns[entry["handler"]["name"]] = get_type_hints(handler.handle)["return"]
    return returns


def unmapped_returns(contract: dict[str, Any], returns: dict[str, Any]) -> list[str]:
    """Returned models the runtime kernel would send to the first publish topic.

    The kernel publishes a returned model to the topic published_events maps its
    class (name without "Model") to, and to the first publish topic when none is
    mapped. A None return publishes nothing.
    """
    mapped = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    publish = set(contract["event_bus"]["publish_topics"])
    return sorted(
        {
            kind.__name__.removeprefix("Model")
            for kind in returns.values()
            if kind is not type(None)
            and mapped.get(kind.__name__.removeprefix("Model")) not in publish
        }
    )


def fallback_topic_consumers(contract: dict[str, Any], node_name: str) -> list[str]:
    """Other nodes that subscribe to this contract's first publish topic."""
    fallback = contract["event_bus"]["publish_topics"][0]
    consumers: list[str] = []
    for path in sorted(NODES.iterdir()):
        contract_path = path / "contract.yaml"
        if path.name == node_name or not contract_path.is_file():
            continue
        text = contract_path.read_text()
        if fallback not in text:
            continue
        other = yaml.safe_load(text)
        bus = other.get("event_bus") or {} if isinstance(other, dict) else {}
        if fallback in (bus.get("subscribe_topics") or []):
            consumers.append(path.name)
    return consumers


def test_every_routed_entry_returns_nothing_to_the_runtime() -> None:
    returns = routed_returns(_contract())
    assert len(returns) == 5
    assert set(returns.values()) == {type(None)}


def test_no_returned_model_is_left_unmapped() -> None:
    contract = _contract()
    assert unmapped_returns(contract, routed_returns(contract)) == []


def test_the_runtime_fallback_topic_is_consumed_by_no_other_node() -> None:
    assert fallback_topic_consumers(_contract(), MINE) == []


def test_an_entry_returns_nothing_and_the_node_still_publishes_its_own_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    entry = HandlerMergeOverridePrMerged(
        event_publisher=rig.publisher,
        database=rig.database,
        reader=rig.reader,
        clock=rig.clock,
    )
    assert entry.handle(pr_merged_event()) is None
    assert len(rig.publisher.on(READ_SUFFIX)) == 1


def test_control_a_returned_result_model_is_reported_unmapped() -> None:
    returns = {"HandlerMergeOverridePrMerged": ModelMergeOverrideResult}
    assert unmapped_returns(_contract(), returns) == ["MergeOverrideResult"]


def test_control_the_landing_request_topic_first_reports_its_consumer() -> None:
    contract = deepcopy(_contract())
    topics = contract["event_bus"]["publish_topics"]
    topics.remove(READ_REQUEST)
    topics.insert(0, READ_REQUEST)
    assert "node_pr_landing_github_effect" in fallback_topic_consumers(contract, MINE)
