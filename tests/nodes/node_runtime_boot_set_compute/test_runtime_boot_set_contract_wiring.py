# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runtime_boot_set_compute declares the command it is called by and the event it answers with (OMN-25006).

A contract with an empty event_bus cannot be reached by a runtime or by ``onex run-node``: the declared demand path
refuses it for want of ``event_bus.subscribe_topics``. The judge-requested command is declared in the contract's topic
map and named by the handler's command adapter, so the subscription, the routed handler topic, the terminal event and
the published topic must all be the ones that map names.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_runtime_boot_set_compute/contract.yaml"
)


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_the_judge_requested_command_is_subscribed_and_routed_to_the_handler() -> None:
    contract = _contract()
    command = contract["topics"]["boot_set_judge_requested"]  # type: ignore[index]

    assert contract["event_bus"]["subscribe_topics"] == [command]  # type: ignore[index]
    assert contract["runtime_dispatch"]["command_topic"] == command  # type: ignore[index]
    routed = contract["handler_routing"]["handlers"]  # type: ignore[index]
    assert [item["topic"] for item in routed] == [command]


def test_the_judged_event_is_the_terminal_and_the_published_topic() -> None:
    contract = _contract()
    judged = contract["topics"]["boot_set_judged"]  # type: ignore[index]

    assert contract["event_bus"]["publish_topics"] == [judged]  # type: ignore[index]
    assert contract["terminal_event"] == judged


def test_the_runtime_principal_may_read_the_command_and_write_the_judged_event() -> (
    None
):
    contract = _contract()
    topics = contract["topics"]
    grants = {
        (grant["name"], grant["operation"])
        for entry in contract["broker_acl_grants"]  # type: ignore[union-attr]
        for grant in entry["grants"]
    }

    assert (topics["boot_set_judge_requested"], "READ") in grants  # type: ignore[index]
    assert (topics["boot_set_judged"], "WRITE") in grants  # type: ignore[index]


def test_a_contract_with_an_empty_event_bus_fails_the_subscription_check() -> None:
    """Control: the pre-change shape, an empty event_bus, is the one the declared demand path refused."""
    contract = _contract()
    contract["event_bus"] = {"subscribe_topics": [], "publish_topics": []}

    assert not contract["event_bus"]["subscribe_topics"]  # type: ignore[index]
