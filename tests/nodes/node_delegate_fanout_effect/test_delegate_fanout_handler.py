# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behavioral ports of the former Workflow tests; the canonical handler is mandatory.

JS parser/agent-instruction assertions became direct dispatch assertions because no
agent or generated shell executes this workflow anymore (OMN-20678).
"""

from __future__ import annotations

import json
import threading
import time
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal
from pydantic import JsonValue, ValidationError

from omnimarket.nodes.node_delegate_fanout_effect.handlers.handler_delegate_fanout import (
    IMPERATIVE_VERBS,
    READY_CONTROL_PROMPT,
    HandlerDelegateFanout,
    admit,
    delegate_argv,
    hard_bound_seconds,
    read_receipt,
)
from omnimarket.nodes.node_delegate_fanout_effect.models.model_fanout_request import (
    ModelFanoutItem,
    ModelFanoutRequest,
)

REPO = Path(__file__).resolve().parents[3]
NODE = REPO / "src/omnimarket/nodes/node_delegate_fanout_effect"
ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/delegate_fanout_parity.json").read_text()
)


@pytest.mark.parametrize("index", range(len(ORACLE["items"])))
def test_admission_matches_previous_workflow(index: int) -> None:
    item = ModelFanoutItem.model_validate(ORACLE["items"][index])
    assert admit(item) == ORACLE["expected"]["admission"][index]


def test_ready_control_remains_admitted_and_contract_declared() -> None:
    contract = REPO / "src/omnimarket/configs/task_class_contracts.v1.yaml"
    assert contract.is_file(), contract
    data = yaml.safe_load(contract.read_text())
    import re

    patterns = data["response_shape_directives"]["exact_literal"]
    assert any(re.search(pattern, READY_CONTROL_PROMPT, re.I) for pattern in patterns)
    assert "reply" in IMPERATIVE_VERBS
    assert admit(ModelFanoutItem(label="control", prompt=READY_CONTROL_PROMPT)) == {
        "ok": True
    }


@pytest.mark.parametrize(
    ("timeout", "bound"), [(None, 360), (1, 61), (240, 300), (300, 360)]
)
def test_hard_bound_scales(timeout: int | None, bound: int) -> None:
    assert hard_bound_seconds(ModelFanoutItem(timeout_s=timeout)) == bound


def test_invocation_uses_sanctioned_wrapper_and_explicit_lane(tmp_path: Path) -> None:
    prompt = "Summarize `$SECRET` $(exit 8)\nONEX_FANOUT_PROMPT_EOF"
    item = ModelFanoutItem(
        label="a",
        prompt=prompt,
        task_type="document",
        timeout_s=240,
        criteria=["one", "two"],
        response_contract={"type": "object"},
    )
    argv = delegate_argv(item, tmp_path, "dev", tmp_path)
    assert argv[:4] == [
        "bash",
        str(tmp_path / "omnibase_infra/scripts/onex"),
        "delegate",
        prompt,
    ]
    for key, value in [
        ("--bus", "kafka"),
        ("--locus", "deployed-lane"),
        ("--lane", "dev"),
        ("--omnibase-path", str(tmp_path)),
        ("--state-root", str(tmp_path)),
        ("--timeout", "240"),
        ("--task-type", "document"),
    ]:
        assert argv[argv.index(key) + 1] == value
    assert argv.count("--criteria") == 2
    assert json.loads((tmp_path / "contract.json").read_text()) == {"type": "object"}
    assert not set(argv) & {"--retry", "--retries", "--max-retries"}


@pytest.mark.parametrize(
    ("stdout", "stderr", "want"),
    [
        ("first\nlast\n", "error", "last"),
        ("", "stderr only\n", "stderr only"),
        ("", "", "(no output on stdout or stderr)"),
    ],
)
def test_no_receipt_keeps_evidence(
    tmp_path: Path, stdout: str, stderr: str, want: str
) -> None:
    (tmp_path / "stdout.txt").write_text(stdout)
    (tmp_path / "stderr.txt").write_text(stderr)
    row = read_receipt(tmp_path)
    assert row["terminal"] == "NO_RECEIPT"
    assert row["last_stdout_line"] == want
    assert row["correlation_id"] is None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("killed", [False, True])
def test_terminal_is_receipt_fact_even_after_kill(
    tmp_path: Path, status: str, killed: bool
) -> None:
    path = tmp_path / "runs/r-1/receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "status": "failed",
                "correlation_id": "c-1",
                "run_id": "r-1",
                "cost_usd": 8,
                "receipt": {
                    "duration_ms": 10,
                    "result": {
                        "terminal_payload": {
                            "payload": {
                                "status": status,
                                "model_name": "model",
                                "quality_score": 0.9,
                                "terminal_failure_cause": "quota",
                                "response": "Paris",
                                "metrics": {"cost_usd": 0},
                            }
                        }
                    },
                },
            }
        )
    )
    if killed:
        (tmp_path / "hard_timeout.txt").write_text(
            "hard timeout after 360s: wrapper did not return"
        )
    row = read_receipt(tmp_path)
    assert row["terminal"] == status
    assert row["correlation_id"] == "c-1"
    assert row["response"] == "Paris"
    assert row["cost_usd"] == 0
    assert row["quality"] == 0.9
    assert row["wall_ms"] == 10
    assert ("hard timeout" in str(row["terminal_failure_cause"])) == killed


def test_hard_timeout_without_receipt_is_visible(tmp_path: Path) -> None:
    reason = "hard timeout after 360s: wrapper did not return"
    (tmp_path / "hard_timeout.txt").write_text(reason)
    (tmp_path / "stdout.txt").write_text("unrelated output")
    row = read_receipt(tmp_path)
    assert row["terminal"] == "NO_RECEIPT"
    assert row["last_stdout_line"] == reason
    assert (
        row["terminal_failure_cause"]
        == "hard timeout: the wrapper did not return within the bound"
    )


def test_receipt_envelope_fallbacks(tmp_path: Path) -> None:
    path = tmp_path / "runs/r-1/receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"status": "failed", "model": "fallback", "cost_usd": 3})
    )
    row = read_receipt(tmp_path)
    assert row["terminal"] == "failed"
    assert row["run_id"] == "r-1"
    assert row["model"] == "fallback"
    assert row["cost_usd"] == 3


def test_refused_items_are_rows_and_never_dispatched(tmp_path: Path) -> None:
    calls: list[str] = []

    def delegate(item: ModelFanoutItem, root: Path, lane: str) -> dict[str, JsonValue]:
        calls.append(item.label)
        return {"terminal": "failed", "correlation_id": "c-1", "response": ""}

    request = ModelFanoutRequest(
        items=[
            ModelFanoutItem(label="a", prompt="Name one"),
            ModelFanoutItem(label="bad", prompt="The task"),
            ModelFanoutItem(label="b", prompt="Name two"),
        ],
        state_root=tmp_path,
    )
    result = HandlerDelegateFanout(delegate).handle(request)
    assert calls == ["a", "b"]
    assert [r.label for r in result.rows] == ["bad", "a", "b"]
    assert result.admitted == 2
    assert result.refused == 1
    assert result.failed == 3
    assert result.receipts == 2
    assert (
        result.closeout_fragment
        == "delegate_receipts=2 correlation_ids=c-1,c-1 failed=3"
    )
    assert "REFUSED_PRECHECK" in result.markdown_table


def test_bounded_parallelism_and_chunk_barrier(tmp_path: Path) -> None:
    lock = threading.Lock()
    active = 0
    peak = 0
    finished: list[str] = []
    roots: list[Path] = []

    def delegate(item: ModelFanoutItem, root: Path, lane: str) -> dict[str, JsonValue]:
        nonlocal active, peak
        with lock:
            if item.label == "2":
                assert set(finished) == {"0", "1"}
            active += 1
            peak = max(peak, active)
            roots.append(root)
        time.sleep(0.03)
        with lock:
            active -= 1
            finished.append(item.label)
        return {"terminal": "completed", "response": "ok"}

    request = ModelFanoutRequest(
        items=[ModelFanoutItem(label=str(i), prompt="Name x") for i in range(3)],
        max_parallel=2,
        state_root=tmp_path,
    )
    result = HandlerDelegateFanout(delegate).handle(request)
    assert peak == 2
    assert len(roots) == len(set(roots)) == 3
    assert [r.label for r in result.rows] == ["0", "1", "2"]
    assert result.failed == 0


@pytest.mark.parametrize(
    "data", [{"items": []}, {"items": [{"label": "a"}], "max_parallel": 0}]
)
def test_invalid_batch_is_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelFanoutRequest.model_validate(data)


def test_registry_and_contract_are_canonical() -> None:
    entry = next(
        ep
        for ep in entry_points(group="onex.nodes")
        if ep.name == "node_delegate_fanout_effect"
    )
    assert entry.load() is not None
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["handler"]["class"] == "HandlerDelegateFanout"
    runtime = contract["runtime_dispatch"]
    assert runtime["command_topic"] in contract["event_bus"]["subscribe_topics"]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    handler_source = (NODE / "handlers/handler_delegate_fanout.py").read_text()
    assert "onex.cmd." not in handler_source
    assert "onex.evt." not in handler_source


def test_shared_runtime_loads_the_contract_and_handler(tmp_path: Path) -> None:
    import asyncio

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


def test_real_subprocess_exit_zero_without_receipt_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_delegate_fanout_effect.handlers import (
        handler_delegate_fanout,
    )

    wrapper = tmp_path / "registry/omnibase_infra/scripts/onex"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/bash\necho "refused before publish"\nexit 0\n')
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "registry"))
    result = handler_delegate_fanout.dispatch_item(
        ModelFanoutItem(label="a", prompt="Name x"), tmp_path / "item", "dev"
    )
    assert result["terminal"] == "NO_RECEIPT"
    assert result["last_stdout_line"] == "refused before publish"


def test_real_subprocess_hard_bound_preserves_no_receipt_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_delegate_fanout_effect.handlers import (
        handler_delegate_fanout,
    )

    wrapper = tmp_path / "registry/omnibase_infra/scripts/onex"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/bash\nsleep 60\n")
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "registry"))
    monkeypatch.setattr(
        handler_delegate_fanout, "hard_bound_seconds", lambda _item: 0.02
    )
    result = handler_delegate_fanout.dispatch_item(
        ModelFanoutItem(label="a", prompt="Name x"), tmp_path / "item", "dev"
    )
    assert result["terminal"] == "NO_RECEIPT"
    assert (
        result["last_stdout_line"] == "hard timeout after 0.02s: wrapper did not return"
    )


def test_contract_declares_the_exact_bus_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    runtime = contract["runtime_dispatch"]
    transport = contract["delegation_transport"]
    assert (
        runtime["command_topic"] == "onex.cmd.omnimarket.delegation-fanout-requested.v1"
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.delegation-fanout-requested.v1"
    ]
    assert runtime["terminal_events"] == {
        "success": "onex.evt.omnimarket.delegation-fanout-completed.v1",
        "failure": "onex.evt.omnimarket.delegation-fanout-failed.v1",
    }
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.delegation-fanout-completed.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.delegation-fanout-completed.v1"
    )
    assert transport["request_topic"] == "onex.cmd.omnimarket.delegate-skill.v1"
    assert transport["retries"] == 0
