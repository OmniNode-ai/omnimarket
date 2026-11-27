# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled node subscribes to the runtime tick and declares the flush topics so provisioning creates them."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
CONTRACT = ROOT / "node_tenant_outbox_flush_scheduled_effect/contract.yaml"
FLUSH = ROOT / "node_tenant_outbox_flush_effect/contract.yaml"


def test_subscription_and_terminal_event() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    subs = loaded["input_subscriptions"]
    assert [(s["topic"], s["operation"]) for s in subs] == [
        ("onex.intent.platform.runtime-tick.v1", "tenant_outbox_flush.scheduled_tick")
    ]
    assert loaded["terminal_event"] in loaded["event_bus"]["publish_topics"]


def test_flush_topics_are_declared_where_provisioning_reads_them() -> None:
    mine = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["topics"][
        "tenant_outbox_flush"
    ]
    flush = yaml.safe_load(FLUSH.read_text(encoding="utf-8"))["topics"][
        "tenant_outbox_flush"
    ]
    assert mine == flush
    assert mine["completed"] == "onex.evt.omnimarket.tenant-outbox-flush-completed.v1"
