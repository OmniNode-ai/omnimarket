# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract's declared topics for the dev signed-in read node (OMN-24345).

The terminal event the contract names is the completed topic the node declares for its answer, the failed topic sits beside
it, and the command topic carries the same stem. Consumers read these names from the contract, so a rename of one alone
fails here. The failing controls rename one name.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from ._support import CONTRACT_PATH

pytestmark = pytest.mark.unit

_STEM = "dev-signed-in-read"


def topic_problems(contract: dict[str, Any]) -> list[str]:
    topics = contract["topics"]
    found: list[str] = []
    if contract["terminal_event"] != topics["completed"]:
        found.append("terminal_event differs from topics.completed")
    if topics["completed"] != f"onex.evt.omnimarket.{_STEM}-completed.v1":
        found.append("topics.completed is not the completed topic of the node's stem")
    if topics["failed"] != f"onex.evt.omnimarket.{_STEM}-failed.v1":
        found.append("topics.failed is not the failed topic of the node's stem")
    if topics["command"] != f"onex.cmd.omnimarket.{_STEM}-requested.v1":
        found.append("topics.command is not the requested topic of the node's stem")
    if set(contract["externally_consumed_topics"]) != {
        topics["completed"],
        topics["failed"],
    }:
        found.append(
            "externally_consumed_topics are not the completed and failed topics"
        )
    return found


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return document


def test_contract_topics_agree() -> None:
    assert topic_problems(_contract()) == []
    assert (
        _contract()["terminal_event"]
        == "onex.evt.omnimarket.dev-signed-in-read-completed.v1"
    )


@pytest.mark.parametrize("name", ["completed", "failed", "command"])
def test_a_renamed_topic_is_reported(name: str) -> None:
    contract = _contract()
    contract["topics"][name] = "onex.evt.omnimarket.renamed.v1"
    assert topic_problems(contract)


def test_a_renamed_terminal_event_is_reported() -> None:
    contract = _contract()
    contract["terminal_event"] = "onex.evt.omnimarket.renamed.v1"
    assert topic_problems(contract) == ["terminal_event differs from topics.completed"]
