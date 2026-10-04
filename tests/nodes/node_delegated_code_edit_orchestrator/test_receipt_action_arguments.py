# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loop receipt keeps each action's full arguments and per-write digests."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, cast

import pytest

from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FIX,
    FakePorts,
    _a,
    _interrupted_ports,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _actions(receipt: dict[str, object], turn: int) -> list[dict[str, Any]]:
    turns = cast(list[dict[str, Any]], receipt["turns"])
    return cast(list[dict[str, Any]], turns[turn - 1]["actions"])


LONG = "".join(f"line {n} of a long generated body\n" for n in range(400))


def _run_scripted() -> tuple[FakePorts, dict[str, object]]:
    ports = FakePorts(
        [
            _reply(
                1,
                _a("view", path="src/m.py"),
                _a("write", file_path="src/big.py", content=LONG),
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="return 0",
                    new_string="return a + b",
                ),
                _a("write", file_path="README.md", content="x\n"),
                _a(
                    "replace_in_files",
                    glob="src/*.py",
                    old_string="line",
                    new_string="ln",
                ),
            ),
            _reply(2, _a("finish", summary="done")),
        ],
        check_passes=[True],
    )
    request = _request(max_turns=4)
    HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    return ports, ports.receipts[request.correlation_id]


def test_receipt_schema_is_v2() -> None:
    _, receipt = _run_scripted()
    assert receipt["schema"] == "delegated-code-edit-loop-receipt.v2"


def test_every_action_keeps_untruncated_arguments() -> None:
    _, receipt = _run_scripted()
    first = _actions(receipt, 1)
    assert first[0]["arguments"] == {"path": "src/m.py"}
    assert first[1]["arguments"]["content"] == LONG
    assert len(first[1]["output"]) <= 2_000 < len(LONG)
    assert first[2]["arguments"]["old_string"] == "return 0"
    assert first[2]["arguments"]["new_string"] == "return a + b"
    assert first[4]["arguments"]["glob"] == "src/*.py"
    assert _actions(receipt, 2)[0]["arguments"] == {"summary": "done"}


def test_written_files_carry_the_sha256_after_the_action() -> None:
    ports, receipt = _run_scripted()
    first = _actions(receipt, 1)
    assert first[1]["written_sha256"] == {"src/big.py": _sha(ports.files["src/big.py"])}
    # the later glob replace rewrote big.py and m.py, so the digest is not the
    # write's own content
    assert first[1]["written_sha256"]["src/big.py"] == _sha(LONG)
    assert set(first[4]["written_sha256"]) == {"src/big.py"}
    assert first[4]["written_sha256"]["src/big.py"] == _sha(ports.files["src/big.py"])
    assert first[2]["written_sha256"] == {
        "src/m.py": _sha("def add(a, b):\n    return a + b\n")
    }


def test_reads_refused_and_finish_actions_carry_an_empty_digest_map() -> None:
    _, receipt = _run_scripted()
    first = _actions(receipt, 1)
    assert first[0]["written_sha256"] == {}
    assert first[3]["refused"] is True
    assert first[3]["written_sha256"] == {}
    assert _actions(receipt, 2)[0]["written_sha256"] == {}


def test_resume_of_a_v1_receipt_still_works() -> None:
    ports = _interrupted_ports()
    request = _request()
    handler = HandlerDelegatedCodeEditOrchestrator(ports)
    handler.run(request)
    v1 = copy.deepcopy(ports.receipts[request.correlation_id])
    v1["schema"] = "delegated-code-edit-loop-receipt.v1"
    for turn in cast(list[dict[str, Any]], v1["turns"]):
        for action in turn["actions"]:
            action.pop("arguments", None)
            action.pop("written_sha256", None)
    ports.receipts[request.correlation_id] = v1
    ports.turns = [_reply(3, _a("finish", summary="fixed"))]
    ports.check_passes = [True]
    result = handler.run(request, resume=True)
    assert result.status == EnumCodeEditStatus.ACCEPTED
    receipt = ports.receipts[request.correlation_id]
    assert receipt["schema"] == "delegated-code-edit-loop-receipt.v2"
    # the v1 turns stay without arguments; the new turn carries them
    assert "arguments" not in _actions(receipt, 1)[0]
    assert _actions(receipt, 3)[0]["arguments"] == {"summary": "fixed"}


def test_resume_of_a_v2_receipt_restores_full_arguments() -> None:
    ports = _interrupted_ports()
    request = _request()
    handler = HandlerDelegatedCodeEditOrchestrator(ports)
    handler.run(request)
    ports.turns = [_reply(3, _a("finish", summary="fixed"))]
    ports.check_passes = [True]
    handler.run(request, resume=True)
    receipt = ports.receipts[request.correlation_id]
    kept = _actions(receipt, 2)[0]
    assert kept["arguments"]["content"] == FIX
    assert kept["written_sha256"] == {"src/m.py": _sha(FIX)}
    calls = cast(list[dict[str, Any]], ports.transcript["calls"])
    assert json.loads(calls[1]["arguments_json"])["content"] == FIX
