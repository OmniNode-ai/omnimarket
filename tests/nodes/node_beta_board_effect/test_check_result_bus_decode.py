# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""beta_board: the bus message decode, and the topic living only in the contract (OMN-26723)."""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.nodes.node_beta_board_effect.handlers.adapter_check_result_bus import (
    decode_record,
)

RECORD = {
    "check_id": "C16",
    "run_id": "r1",
    "created_at": "2026-11-28T13:30:07+00:00",
    "event": "schedule",
    "verdict": "PASS",
}


def test_a_flat_record_decodes() -> None:
    got = decode_record(json.dumps(RECORD).encode())
    assert got is not None
    assert (got.check_id, got.verdict, got.event) == ("C16", "PASS", "schedule")


def test_an_enveloped_record_decodes() -> None:
    got = decode_record(json.dumps({"payload": RECORD}).encode())
    assert got is not None
    assert got.check_id == "C16"


def test_garbage_and_foreign_messages_are_not_records() -> None:
    assert decode_record(b"not json") is None
    assert decode_record(b"[1]") is None
    assert decode_record(json.dumps({"other": 1}).encode()) is None


def test_no_handler_holds_the_topic_as_a_constant() -> None:
    root = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_beta_board_effect"
    )
    offenders = [
        str(p)
        for p in root.rglob("*.py")
        if "delegation-check-probe-completed" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
