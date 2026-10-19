# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Safety controls for the Bash compute, before a client may replace output."""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

from omnimarket.nodes.node_tool_result_reduce_compute.handlers.handler_tool_result_reduce import (
    HandlerToolResultReduce,
)
from omnimarket.nodes.node_tool_result_reduce_compute.models.model_tool_result_reduce import (
    ModelToolResultReduceRequest,
    ModelToolResultReduceResult,
)
from tests.runtime_local_compat import RuntimeLocal

_ROOT = Path(__file__).resolve().parents[3]
_NODE = _ROOT / "src/omnimarket/nodes/node_tool_result_reduce_compute"


@pytest.mark.parametrize("exit_status", [1, 2, 127, 137, -9])
@pytest.mark.parametrize(
    "output",
    [
        "",
        "all tests passed\n",  # The text must never override the exit status.
        "disk quota reached\n  /data/cache\nretry aborted\n",
        "ERROR: first\nERROR: first\nTraceback:\n    line 7\nValueError: bad\n",
        "prelude\r\n\n" + "\n" * 500 + "échec: Ω\x1b[0m\n",
    ],
    ids=[
        "empty",
        "misleading-success",
        "unknown-diagnostics",
        "duplicate-errors",
        "mixed-line-endings",
    ],
)
async def test_failed_result_preserves_status_and_entire_output(
    exit_status: int, output: str
) -> None:
    result = await HandlerToolResultReduce().handle(
        ModelToolResultReduceRequest(
            tool_use_id="toolu_failed", exit_status=exit_status, output=output
        )
    )
    header, body = result.reduced_text.split("\n", 1)
    assert header == f"Bash exit_status={exit_status}"
    assert body == output
    assert result.exit_status == exit_status
    assert result.error_lines == tuple(line for line in output.split("\n") if line)
    assert result.original_sha256 == hashlib.sha256(output.encode("utf-8")).hexdigest()


async def test_success_reduces_only_empty_lf_runs_and_keeps_every_diagnostic() -> None:
    lines = [
        "unrecognised diagnostic without an error keyword",
        "ERROR: command exited zero anyway",
        "ERROR: command exited zero anyway",
        "  diagnostic continuation  ",
        "警告: preserved",
    ]
    output = ("\n" * 500).join(lines) + "\n"
    result = await HandlerToolResultReduce().handle(
        ModelToolResultReduceRequest(
            tool_use_id="toolu_success", exit_status=0, output=output
        )
    )
    assert result.reduced_text.startswith("Bash exit_status=0\n")
    assert len(result.reduced_text) < len(output)
    assert result.error_lines == tuple(lines)
    # Preserve duplicate lines in sequence, not merely membership in a set.
    remaining = result.reduced_text
    for line in lines:
        _, separator, remaining = remaining.partition(line)
        assert separator == line
    assert result.original_sha256 == hashlib.sha256(output.encode()).hexdigest()
    assert result.original_record_id is None
    assert "tool_result_read" not in result.reduced_text


@pytest.mark.parametrize(
    "output",
    ["", "one\n\ntwo\n", "a\r\n\r\nb\r\n", "\t\n  \n", "x\u2028y\x00z"],
)
async def test_short_and_non_lf_content_is_unchanged(output: str) -> None:
    result = await HandlerToolResultReduce().handle(
        ModelToolResultReduceRequest(
            tool_use_id="toolu_short", exit_status=0, output=output
        )
    )
    assert result.reduced_text == "Bash exit_status=0\n" + output


async def test_large_non_empty_output_is_not_truncated_or_deduplicated() -> None:
    output = "a line that could be a diagnostic\n" * 2000
    result = await HandlerToolResultReduce().handle(
        ModelToolResultReduceRequest(
            tool_use_id="toolu_large", exit_status=0, output=output
        )
    )
    assert result.reduced_text == "Bash exit_status=0\n" + output
    assert len(result.error_lines) == 2000


async def test_contract_dispatch_uses_typed_handler_and_restricted_topics() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    handler_spec = contract["handler"]
    handler_class = getattr(
        importlib.import_module(handler_spec["module"]), handler_spec["class"]
    )
    request_type = contract["input_model"]
    request_class = getattr(
        importlib.import_module(request_type["module"]), request_type["name"]
    )
    request = request_class.model_validate(
        {
            "tool_use_id": "toolu_contract",
            "exit_status": 23,
            "output": "quota reached\n",
        }
    )
    first = await handler_class().handle(request)
    second = await handler_class().handle(request)
    assert isinstance(first, ModelToolResultReduceResult)
    assert first == second
    assert first.exit_status == 23
    assert first.reduced_text == "Bash exit_status=23\nquota reached\n"
    topics = contract["event_bus"]
    assert topics["subscribe_topics"] == [
        "onex.cmd.omniintelligence.tool-result-reduce-compute-requested.v1"
    ]
    assert topics["publish_topics"] == [
        "onex.cmd.omniintelligence.tool-result-reduce-compute-completed.v1"
    ]
    assert (
        contract["runtime_dispatch"]["command_topic"] == topics["subscribe_topics"][0]
    )
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.cmd.omniintelligence.tool-result-reduce-compute-completed.v1"
    }
    assert contract["terminal_event"] == topics["publish_topics"][0]
    assert contract["externally_consumed_topics"] == topics["publish_topics"]
    assert any(
        entry.name == "node_tool_result_reduce_compute"
        for entry in entry_points(group="onex.nodes")
    )


@pytest.mark.parametrize("exit_status", [0, 23, -9])
async def test_result_round_trips_through_runtime_bus(
    tmp_path: Path, exit_status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    output = "diagnostic without a keyword\n" + "\n" * 500 + "duplicate\nduplicate\n"
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "tool_use_id": "toolu_bus",
                "exit_status": exit_status,
                "output": output,
            }
        )
    )
    runtime = RuntimeLocal(
        workflow_path=_NODE / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    assert any(
        record.getMessage().startswith(
            "LocalRuntimeBusAdapter: published to "
            "onex.cmd.omniintelligence.tool-result-reduce-compute-completed.v1 "
        )
        for record in caplog.records
    )
    result = runtime.handler_result
    assert isinstance(result, ModelToolResultReduceResult)
    assert result.exit_status == exit_status
    assert result.error_lines == (
        "diagnostic without a keyword",
        "duplicate",
        "duplicate",
    )
    assert result.original_sha256 == hashlib.sha256(output.encode()).hexdigest()
    written = json.loads((tmp_path / "state/workflow_result.json").read_text())
    terminal = written["terminal_payload"]
    assert terminal["exit_status"] == exit_status
    assert terminal["reduced_text"] == result.reduced_text
    assert terminal["original_record_id"] is None
    if exit_status:
        assert terminal["reduced_text"] == f"Bash exit_status={exit_status}\n{output}"
    else:
        assert len(terminal["reduced_text"]) < len(output)


async def test_runtime_refuses_an_ambiguous_status_without_a_result(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps({"tool_use_id": "toolu_bad", "exit_status": None, "output": "ok"})
    )
    runtime = RuntimeLocal(
        workflow_path=_NODE / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None


@pytest.mark.parametrize(
    "override",
    [
        {"tool_name": "Read"},
        {"tool_use_id": ""},
        {"exit_status": None},
        {"exit_status": True},
        {"exit_status": "1"},
        {"output": b"bytes"},
        {"unknown": "not allowed"},
    ],
)
def test_request_refuses_ambiguous_status_and_other_tools(
    override: dict[str, object],
) -> None:
    raw: dict[str, object] = {
        "tool_use_id": "toolu_invalid",
        "exit_status": 0,
        "output": "",
    }
    with pytest.raises(ValidationError):
        ModelToolResultReduceRequest.model_validate(raw | override)


def test_models_are_frozen() -> None:
    request = ModelToolResultReduceRequest(
        tool_use_id="toolu_frozen", exit_status=1, output="failed"
    )
    with pytest.raises(ValidationError):
        request.exit_status = 0
