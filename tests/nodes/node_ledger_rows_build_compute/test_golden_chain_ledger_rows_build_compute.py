# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Literal argv/refusal parity and contract-to-bus golden/error chains."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

import omnimarket.nodes.node_ledger_rows_build_compute as node_package
from omnimarket.nodes.node_ledger_rows_build_compute.handlers.handler_ledger_rows_build import (
    HandlerLedgerRowsBuild,
)
from omnimarket.nodes.node_ledger_rows_build_compute.models.model_ledger_rows_build import (
    ModelLedgerRowsBuildRequest,
    ModelLedgerRowsBuildResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.ledger-rows-build-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.ledger-rows-build-completed.v1"
HANDLER = HandlerLedgerRowsBuild()


def ask(**fields: Any) -> ModelLedgerRowsBuildResult:
    if "request_data" in fields:
        fields.setdefault("request_mode", "json")
    return HANDLER.handle(ModelLedgerRowsBuildRequest(operation="build", **fields))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    assert (
        getattr(importlib.import_module(binding["module"]), binding["class"])
        is HandlerLedgerRowsBuild
    )
    assert issubclass(node_package.NodeLedgerRowsBuildCompute, NodeCompute)
    for side, model in (
        ("input_model", ModelLedgerRowsBuildRequest),
        ("output_model", ModelLedgerRowsBuildResult),
    ):
        assert (
            getattr(
                importlib.import_module(contract[side]["module"]),
                contract[side]["name"],
            )
            is model
        )
    entry = metadata["entry_points"]["onex.nodes"]["node_ledger_rows_build_compute"]
    assert importlib.import_module(entry) is node_package
    assert metadata["capabilities"]["side_effect_class"] == "none"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"kind": "claim", "lane": "lane-a"}, ["row", "CLAIM", "--lane", "lane-a"]),
        (
            {
                "kind": "STATUS",
                "lane": " lane-a ",
                "ticket": "T-1",
                "actor": "a",
                "model": "m",
                "text": "hello",
            },
            [
                "row",
                "STATUS",
                "--lane",
                " lane-a ",
                "--ticket",
                "T-1",
                "--actor",
                "a",
                "--model",
                "m",
                "--text",
                "hello",
            ],
        ),
        (
            {
                "kind": "terminal",
                "lane": "a",
                "ticket": "ignored",
                "tickets": ["T-2", "T-3"],
                "extra": {
                    "outcome": "failed",
                    "kv": ["delegation=na:solo", "worktree=none"],
                    "skip": None,
                    "empty": [],
                    "value": "",
                },
            },
            [
                "row",
                "TERMINAL",
                "--lane",
                "a",
                "--ticket",
                "T-2",
                "--ticket",
                "T-3",
                "--outcome",
                "failed",
                "--kv",
                "delegation=na:solo",
                "--kv",
                "worktree=none",
                "--value",
                "",
            ],
        ),
        (
            {
                "kind": "FRICTION",
                "lane": "a",
                "ticket": "T-1",
                "tickets": [],
                "actor": "",
                "model": "",
                "text": "",
                "extra": {"cost": "0"},
            },
            ["row", "FRICTION", "--lane", "a", "--ticket", "T-1", "--cost", "0"],
        ),
        (
            {
                "kind": "custom",
                "lane": "a",
                "text": "quote ' | $(ignored) %\nsecond line",
            },
            [
                "row",
                "CUSTOM",
                "--lane",
                "a",
                "--text",
                "quote ' | $(ignored) %\nsecond line",
            ],
        ),
        ({"kind": "", "lane": "a"}, ["row", "", "--lane", "a"]),
    ],
)
def test_direct_build_preserves_argv(
    fields: dict[str, Any], expected: list[str]
) -> None:
    result = ask(**fields)
    assert result.argv == expected
    assert result.admitted is True
    assert result.refusal is None


@pytest.mark.parametrize("kind", ["ruling", "OPERATOR-CONSENT"])
def test_operator_only_rows_refuse_before_blank_lane(kind: str) -> None:
    result = ask(kind=kind, lane="")
    assert result.argv == []
    assert result.admitted is False
    assert result.refusal == (
        f"a {kind.upper()} row goes through /omni:operator-ruling, which keeps the words verbatim "
        "and adds the OUT OF SCOPE floor; this typed builder never writes one"
    )


@pytest.mark.parametrize("lane", ["", " \t\n"])
def test_blank_lane_refuses(lane: str) -> None:
    assert (
        ask(kind="STATUS", lane=lane).refusal == "lane is required and may not be blank"
    )


@pytest.mark.parametrize("kind", ["claim", "STATUS", "TERMINAL", "FRICTION"])
def test_json_request_accepts_each_supported_kind(kind: str) -> None:
    result = ask(
        request_data={
            "kind": kind,
            "lane": "a",
            "ticket": None,
            "tickets": ["T-1"],
            "actor": "a",
            "model": "m",
            "text": "a\nb",
            "extra": {"kv": ["one=1", "two=2"], "cost": "0", "skip": None},
        }
    )
    assert result.argv == [
        "row",
        kind.upper(),
        "--lane",
        "a",
        "--ticket",
        "T-1",
        "--actor",
        "a",
        "--model",
        "m",
        "--kv",
        "one=1",
        "--kv",
        "two=2",
        "--cost",
        "0",
        "--text",
        "a\nb",
    ]
    assert result.refusal is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (None, "request must be a JSON object"),
        ([], "request must be a JSON object"),
        ({"z": 1, "a": 2}, "unknown request fields: a, z"),
        ({}, "kind must be a string"),
        ({"kind": 5}, "kind must be a string"),
        ({"kind": "STATUS"}, "lane must be a string"),
        ({"kind": "STATUS", "lane": None}, "lane must be a string"),
        (
            {"kind": "RULING", "lane": "a"},
            "request kind must be CLAIM, STATUS, TERMINAL or FRICTION",
        ),
        (
            {"kind": "OPERATOR-CONSENT", "lane": "a"},
            "request kind must be CLAIM, STATUS, TERMINAL or FRICTION",
        ),
        (
            {"kind": "CUSTOM", "lane": "a"},
            "request kind must be CLAIM, STATUS, TERMINAL or FRICTION",
        ),
        (
            {"kind": "STATUS", "lane": "a", "ticket": 1},
            "ticket must be a string or null",
        ),
        (
            {"kind": "STATUS", "lane": "a", "actor": False},
            "actor must be a string or null",
        ),
        (
            {"kind": "STATUS", "lane": "a", "model": []},
            "model must be a string or null",
        ),
        ({"kind": "STATUS", "lane": "a", "text": {}}, "text must be a string or null"),
        (
            {"kind": "STATUS", "lane": "a", "tickets": "T-1"},
            "tickets must be a list of strings or null",
        ),
        (
            {"kind": "STATUS", "lane": "a", "tickets": ["T-1", 1]},
            "tickets must be a list of strings or null",
        ),
        (
            {"kind": "STATUS", "lane": "a", "ticket": "T-1", "tickets": ["T-2"]},
            "pass ticket or tickets, not both",
        ),
        (
            {"kind": "STATUS", "lane": "a", "extra": []},
            "extra must be an object or null",
        ),
        (
            {"kind": "STATUS", "lane": "a", "extra": {"--kv": "a"}},
            "extra keys must be ledger flag names without leading dashes",
        ),
        (
            {"kind": "STATUS", "lane": "a", "extra": {"Upper": "a"}},
            "extra keys must be ledger flag names without leading dashes",
        ),
        (
            {"kind": "STATUS", "lane": "a", "extra": {"kv": 1}},
            "extra.kv must be a string, list of strings or null",
        ),
        (
            {"kind": "STATUS", "lane": "a", "extra": {"kv": [False]}},
            "extra.kv must be a string, list of strings or null",
        ),
        ({"kind": "STATUS", "lane": " "}, "lane is required and may not be blank"),
    ],
)
def test_json_request_refusals(data: Any, message: str) -> None:
    result = ask(request_data=data)
    assert result.argv == []
    assert result.admitted is False
    assert result.refusal == message


@pytest.mark.parametrize(
    "fields", [{"ticket": "", "tickets": ["T-2"]}, {"ticket": "T-2", "tickets": []}]
)
def test_json_ticket_conflict_uses_truthiness(fields: dict[str, Any]) -> None:
    assert ask(request_data={"kind": "STATUS", "lane": "a", **fields}).argv == [
        "row",
        "STATUS",
        "--lane",
        "a",
        "--ticket",
        "T-2",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "other"},
        {"operation": "build", "kind": 5},
        {"operation": "build", "extra": {"kv": [1]}},
        {"operation": "build", "surprise": 1},
    ],
)
def test_error_chain_refuses_malformed_envelopes(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelLedgerRowsBuildRequest.model_validate(payload)


def test_models_are_frozen_and_result_round_trips() -> None:
    request = ModelLedgerRowsBuildRequest(operation="build", kind="STATUS", lane="a")
    with pytest.raises(ValidationError):
        request.lane = "b"
    assert HANDLER.handle(
        ModelLedgerRowsBuildRequest.model_validate_json(request.model_dump_json())
    ).argv == ["row", "STATUS", "--lane", "a"]
    result = HANDLER.handle(request)
    assert (
        ModelLedgerRowsBuildResult.model_validate_json(result.model_dump_json())
        == result
    )
    with pytest.raises(ValidationError):
        result.admitted = False


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{name}",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "argv", "refusal"),
    [
        (
            {"operation": "build", "kind": "status", "lane": "a", "text": "hello"},
            ["row", "STATUS", "--lane", "a", "--text", "hello"],
            None,
        ),
        (
            {"operation": "build", "request_data": {"kind": "STATUS", "lane": ""}},
            [],
            "lane is required and may not be blank",
        ),
    ],
)
async def test_golden_chain_over_the_bus(
    tmp_path: Path, payload: dict[str, Any], argv: list[str], refusal: str | None
) -> None:
    runtime = await _run(tmp_path, payload, "build")
    result = runtime.handler_result
    assert isinstance(result, ModelLedgerRowsBuildResult)
    assert result.argv == argv
    assert result.refusal == refusal
    assert result.admitted is (refusal is None)


@pytest.mark.asyncio
async def test_error_chain_over_the_bus(tmp_path: Path) -> None:
    input_path = tmp_path / "bad.json"
    input_path.write_text('{"operation":"other"}')
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state-bad",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
