# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the day-one listing, each able to fail for real.

Golden: the staged copy with every read declared publishes one WORKING event on the contract's topic.
Error: a planted undeclared read publishes one NOT_WORKING ``undeclared_private_read`` event.
Named break ``reads-hidden``: ``classify_line`` patched to call every line ``name_only``. Under it the error
chain reads WORKING, so the error test below would go red; the mutation test demands that.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_landing_private_reads_effect.handlers import (
    handler_landing_private_reads as handler_module,
)
from omnimarket.nodes.node_landing_private_reads_effect.handlers.handler_landing_private_reads import (
    TOPIC_LISTED,
    InMemoryPrivateReadsEventSink,
)
from omnimarket.nodes.node_landing_private_reads_effect.runtime_private_reads import run

from .private_reads_support import CONTRACT, write_copy, write_overlay_root

pytestmark = pytest.mark.unit


def _chain(tmp_path: Path, *, planted: bool) -> InMemoryPrivateReadsEventSink:
    copy = tmp_path / "copy"
    write_copy(copy, planted=planted)
    roots = write_overlay_root(tmp_path / "overlays")
    sink = InMemoryPrivateReadsEventSink()
    run(
        {
            "ONEX_SKILL_OVERLAY_ROOTS": str(roots),
            "LANDING_PRIVATE_READS_COPY_ROOT": str(copy),
        },
        sink=sink,
        correlation_id="chain-1",
    )
    return sink


def test_topic_is_the_contract_publish_topic() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["event_bus"]["publish_topics"] == [TOPIC_LISTED]


def test_golden_chain_event(tmp_path: Path) -> None:
    sink = _chain(tmp_path, planted=False)
    assert [
        (e.topic, e.correlation_id, e.verdict, e.failure_code) for e in sink.events
    ] == [(TOPIC_LISTED, "chain-1", "WORKING", None)]
    assert sink.events[0].reads == 2


def test_error_chain_event(tmp_path: Path) -> None:
    sink = _chain(tmp_path, planted=True)
    assert [(e.verdict, e.failure_code, e.undeclared) for e in sink.events] == [
        ("NOT_WORKING", "undeclared_private_read", 1)
    ]


def test_named_break_reads_hidden_turns_the_error_chain_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reads_hidden(*_args: object) -> str:
        return "name_only"

    monkeypatch.setattr(handler_module, "classify_line", reads_hidden)
    sink = _chain(tmp_path, planted=True)
    # The break hides the planted read: the reading would pass a copy it must fail.
    assert [(e.verdict, e.undeclared) for e in sink.events] == [("WORKING", 0)]
