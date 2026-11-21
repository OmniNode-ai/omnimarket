# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab ssh probe node declares the bus route a demand reaches it by, and the grants that route needs (OMN-25456)."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

import yaml

NAME = "node_lab_ssh_probe_compute"
COMMAND = "onex.cmd.omnimarket.lab-ssh-probe-requested.v1"
TERMINAL = "onex.evt.omnimarket.lab-ssh-probe-rendered.v1"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def test_the_runtime_principal_is_granted_exactly_the_route_it_serves() -> None:
    grants = _contract()["broker_acl_grants"]
    assert [g["principal"] for g in grants] == ["${ONEX_ACL_RUNTIME_PRINCIPAL}"]
    pairs = {(g["name"], g["operation"]) for g in grants[0]["grants"]}
    assert pairs == {
        (COMMAND, "READ"),
        (COMMAND, "DESCRIBE"),
        (TERMINAL, "WRITE"),
        (TERMINAL, "DESCRIBE"),
    }


def test_a_demand_names_one_command_topic_and_the_node_stays_pure() -> None:
    contract = _contract()
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["descriptor"]["side_effects"] == []
    assert contract["descriptor"]["runtime_profiles"] == ["effects"]
