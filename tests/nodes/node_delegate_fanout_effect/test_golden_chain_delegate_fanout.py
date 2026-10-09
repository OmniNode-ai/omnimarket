# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for node_delegate_fanout_effect (OMN-20678).

Golden chain: a batch with one refused and two admitted items runs through the
contract-declared handler and yields refused rows first, then admitted rows in
request order, with the closeout fragment and table derived from receipts.

Error chain: a failed terminal is reported as a failed row and is never retried,
and a request the contract rejects never reaches the handler.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from omnibase_core.runtime.runtime_local import RuntimeLocal
from pydantic import JsonValue, ValidationError

from omnimarket.nodes.node_delegate_fanout_effect.handlers.handler_delegate_fanout import (
    HandlerDelegateFanout,
)
from omnimarket.nodes.node_delegate_fanout_effect.models.model_fanout_request import (
    ModelFanoutItem,
    ModelFanoutRequest,
)

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_delegate_fanout_effect"
)


def test_golden_chain_mixed_batch_orders_rows_and_derives_closeout(
    tmp_path: Path,
) -> None:
    def delegate(item: ModelFanoutItem, root: Path, lane: str) -> dict[str, JsonValue]:
        return {
            "terminal": "completed",
            "correlation_id": f"c-{item.label}",
            "response": "ok",
        }

    request = ModelFanoutRequest(
        items=[
            ModelFanoutItem(label="a", prompt="Name one"),
            ModelFanoutItem(label="bad", prompt="The task"),
            ModelFanoutItem(label="b", prompt="Name two"),
        ],
        state_root=tmp_path,
    )
    result = HandlerDelegateFanout(delegate).handle(request)
    assert [row.label for row in result.rows] == ["bad", "a", "b"]
    assert [row.terminal for row in result.rows] == [
        "REFUSED_PRECHECK",
        "completed",
        "completed",
    ]
    assert (
        result.closeout_fragment
        == "delegate_receipts=2 correlation_ids=c-a,c-b failed=1"
    )
    assert result.markdown_table.count("\n") == 4


def test_error_chain_failed_terminal_is_reported_and_not_retried(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def delegate(item: ModelFanoutItem, root: Path, lane: str) -> dict[str, JsonValue]:
        calls.append(item.label)
        return {"terminal": "failed", "correlation_id": "c-1", "response": ""}

    request = ModelFanoutRequest(
        items=[ModelFanoutItem(label="a", prompt="Name one")], state_root=tmp_path
    )
    result = HandlerDelegateFanout(delegate).handle(request)
    assert calls == ["a"]
    assert result.failed == 1
    assert result.receipts == 1


def test_error_chain_invalid_request_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError):
        ModelFanoutRequest.model_validate({"items": []})


def test_golden_chain_runtime_runs_the_declared_handler(tmp_path: Path) -> None:
    payload = tmp_path / "request.json"
    payload.write_text(json.dumps({"items": [{"label": "bad", "prompt": "The task"}]}))
    runtime = RuntimeLocal(
        NODE / "contract.yaml",
        input_path=payload,
        state_root=tmp_path / "state",
        timeout=5,
        backend_overrides={"event_bus": "inmemory"},
    )
    result = asyncio.run(runtime.run_async())
    assert result.value == "completed"
    state = json.loads((tmp_path / "state/workflow_result.json").read_text())
    assert state["handler_result"]["rows"][0]["terminal"] == "REFUSED_PRECHECK"
    assert state["handler_result"]["failed"] == 1
