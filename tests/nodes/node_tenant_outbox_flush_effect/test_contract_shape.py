# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract names the command, the terminal event, the one publish topic and the trigger; the topic is read, not typed."""

from __future__ import annotations

from importlib import resources
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    publish_topic_from_contract,
)

pytestmark = pytest.mark.unit

_NODE = "node_tenant_outbox_flush_effect"
CMD = "onex.cmd.omnimarket.tenant-outbox-flush-requested.v1"
DONE = "onex.evt.omnimarket.tenant-outbox-flush-completed.v1"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_topics_and_trigger() -> None:
    c = _contract()
    assert c["node_type"] == "effect"
    assert c["runtime_profiles"] == ["effects"]
    assert c["event_bus"]["subscribe_topics"] == [CMD]
    assert DONE in c["event_bus"]["publish_topics"]
    assert c["terminal_event"] == DONE
    assert c["runtime_dispatch"]["command_topic"] == CMD
    assert c["runtime_dispatch"]["terminal_events"]["success"] == DONE


def test_publish_topic_is_read_from_the_contract() -> None:
    assert (
        publish_topic_from_contract() == _contract()["topics"]["flush"]["tenant_events"]
    )
    assert publish_topic_from_contract() == "onex.tenant.events"


def test_runtime_principal_may_write_the_tenant_events_topic() -> None:
    grants = {
        (b["principal"], g["name"], g["operation"])
        for b in _contract()["broker_acl_grants"]
        for g in b["grants"]
    }
    assert ("${ONEX_ACL_RUNTIME_PRINCIPAL}", "onex.tenant.events", "WRITE") in grants
