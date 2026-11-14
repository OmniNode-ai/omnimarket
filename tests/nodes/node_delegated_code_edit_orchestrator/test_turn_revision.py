# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24173 AC1: the code edit loop hands the trial driver the revision its endpoint reported.

A turn's revision is read from the delegate run's own receipt. The loop result carries the one revision every
answering turn reported, and none when a turn reported none or two turns reported different ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.delegated_code_edit.loop_ports import _read_delegate_receipt
from omnimarket.models.model_delegated_code_edit import ModelTurnReply
from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.handler_delegated_code_edit_orchestrator import (
    _turn_revision,
)

pytestmark = pytest.mark.unit


def receipt_file(tmp_path: Path, body: dict) -> Path:
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(body))
    return path


def reply(revision: str, run_id: str = "run-1") -> ModelTurnReply:
    return ModelTurnReply(run_id=run_id, ok=True, model_revision=revision)


@pytest.mark.parametrize(
    "body",
    [
        {"model": "m", "model_revision": "sha256:a"},
        {"model": "m", "receipt": {"result": {"model_revision": "sha256:a"}}},
        {
            "model": "m",
            "receipt": {
                "result": {
                    "terminal_payload": {
                        "payload": {"metrics": {"model_revision": "sha256:a"}}
                    }
                }
            },
        },
    ],
)
def test_the_turn_reads_the_revision_the_delegate_receipt_reports(
    tmp_path: Path, body: dict
) -> None:
    assert _read_delegate_receipt(receipt_file(tmp_path, body)).model_revision == (
        "sha256:a"
    )


def test_a_receipt_that_reports_no_revision_reads_empty(tmp_path: Path) -> None:
    assert (
        _read_delegate_receipt(receipt_file(tmp_path, {"model": "m"})).model_revision
        == ""
    )
    assert _read_delegate_receipt(tmp_path / "missing.json").model_revision == ""


def test_the_loop_result_carries_the_one_revision_every_turn_reported() -> None:
    assert _turn_revision([reply("sha256:a"), reply("sha256:a")]) == "sha256:a"


def test_the_loop_result_carries_none_when_a_turn_reported_none() -> None:
    assert _turn_revision([reply("sha256:a"), reply("")]) == ""


def test_the_loop_result_carries_none_when_two_turns_reported_different_ones() -> None:
    assert _turn_revision([reply("sha256:a"), reply("sha256:b")]) == ""


def test_a_turn_that_never_reached_the_delegate_is_not_an_answering_turn() -> None:
    assert _turn_revision([reply("sha256:a"), reply("", run_id="")]) == "sha256:a"
    assert _turn_revision([]) == ""
