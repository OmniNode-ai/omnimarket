# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane rules parity, declared contract bindings, and golden/error bus chains."""

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

import omnimarket.nodes.node_lane_rules_block_compute as node_package
from omnimarket.nodes.node_lane_rules_block_compute.handlers.handler_lane_rules_block import (
    HandlerLaneRulesBlock,
)
from omnimarket.nodes.node_lane_rules_block_compute.models.model_lane_rules_block import (
    ModelLaneRulesBlockRequest,
    ModelLaneRulesBlockResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.lane-rules-block-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.lane-rules-block-completed.v1"
HANDLER = HandlerLaneRulesBlock()


def ask(**fields: Any) -> ModelLaneRulesBlockResult:
    return HANDLER.handle(ModelLaneRulesBlockRequest.model_validate(fields))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    metadata = dict(yaml.safe_load((NODE_DIR / "metadata.yaml").read_text()))
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert handler_type is HandlerLaneRulesBlock
    assert node_package.HandlerLaneRulesBlock is handler_type
    assert issubclass(node_package.NodeLaneRulesBlockCompute, NodeCompute)
    assert {
        entry["operation"] for entry in contract["handler_routing"]["handlers"]
    } == {"render_block", "contract_step", "contract_block", "block_path"}
    for side, model in (
        ("input_model", ModelLaneRulesBlockRequest),
        ("output_model", ModelLaneRulesBlockResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
        assert binding[side] == f"{model.__module__}.{model.__name__}"
    entry = metadata["entry_points"]["onex.nodes"]["node_lane_rules_block_compute"]
    assert importlib.import_module(entry) is node_package


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Run " + "<omni " + "plugin>/scripts/check.py\n",
            'Run "$OMNI_PLUGIN_ROOT"/scripts/check.py\n',
        ),
        (
            "<omni " + "plugin> and <omni " + "plugin>",
            '"$OMNI_PLUGIN_ROOT" and "$OMNI_PLUGIN_ROOT"',
        ),
        ("unchanged café\r\n\n", "unchanged café\r\n\n"),
        ("<omni plugin >", "<omni plugin >"),
        ("", ""),
    ],
)
def test_render_block_matches_original_without_adding_a_newline(
    text: str, expected: str
) -> None:
    assert ask(operation="render_block", block_text=text).model_dump() == {
        "operation": "render_block",
        "text": expected,
        "path": None,
        "exit_code": 0,
        "message": None,
    }


@pytest.mark.parametrize(
    ("ticket", "expected"),
    [
        (
            "TASK-42",
            "Before the first push, write contracts/TASK-42.yaml with a dod_evidence entry per acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped so the hosted runner can run it; the push is refused without it.",
        ),
        (
            "",
            "Before the first push, write contracts/.yaml with a dod_evidence entry per acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped so the hosted runner can run it; the push is refused without it.",
        ),
        (
            " {ticket} ",
            "Before the first push, write contracts/ {ticket} .yaml with a dod_evidence entry per acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped so the hosted runner can run it; the push is refused without it.",
        ),
    ],
)
def test_contract_step_interpolates_ticket_verbatim(ticket: str, expected: str) -> None:
    assert ask(operation="contract_step", ticket=ticket).text == expected


@pytest.mark.parametrize(
    ("ticket", "expected"),
    [
        (
            "TASK-42",
            "Required when this lane may open or push a PR in a repository whose CI runs repo-evidence / dod-verify or deploy-gate (read its .github/workflows CI config):\nBefore the first push, write contracts/TASK-42.yaml with a dod_evidence entry per acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped so the hosted runner can run it; the push is refused without it.",
        ),
        (
            "",
            "Required when this lane may open or push a PR in a repository whose CI runs repo-evidence / dod-verify or deploy-gate (read its .github/workflows CI config):\nBefore the first push, write contracts/.yaml with a dod_evidence entry per acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped so the hosted runner can run it; the push is refused without it.",
        ),
    ],
)
def test_contract_block_matches_original(ticket: str, expected: str) -> None:
    assert ask(operation="contract_block", ticket=ticket).text == expected


@pytest.mark.parametrize("checkout", ["/checkout/project", "/checkout//project/."])
def test_block_path_uses_supplied_checkout_without_accessing_it(checkout: str) -> None:
    assert ask(operation="block_path", checkout_path=checkout).model_dump() == {
        "operation": "block_path",
        "text": None,
        "path": "/checkout/project/lane_rules/lane-rules-block.md",
        "exit_code": 0,
        "message": None,
    }


def test_missing_checkout_returns_resolver_error_unchanged_before_block_check() -> None:
    assert ask(
        operation="block_path",
        resolve_error="onex-ledger resolver: no valid clone",
        block_exists=False,
    ).model_dump() == {
        "operation": "block_path",
        "text": None,
        "path": None,
        "exit_code": 2,
        "message": "onex-ledger resolver: no valid clone",
    }


def test_missing_block_is_refused_with_original_message() -> None:
    assert ask(
        operation="block_path", checkout_path="/checkout/project", block_exists=False
    ).model_dump() == {
        "operation": "block_path",
        "text": None,
        "path": None,
        "exit_code": 2,
        "message": "lane rules block is missing: /checkout/project/lane_rules/lane-rules-block.md",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "other"},
        {"operation": "render_block", "block_text": 5},
        {"operation": "contract_step", "ticket": 5},
        {"operation": "contract_block", "ticket": None},
        {"operation": "block_path"},
        {"operation": "render_block", "surprise": 1},
    ],
)
def test_error_chain_refuses_invalid_requests(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelLaneRulesBlockRequest.model_validate(payload)


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    request = ModelLaneRulesBlockRequest(operation="render_block")
    with pytest.raises(ValidationError, match="frozen_instance"):
        request.ticket = "changed"
    result = HANDLER.handle(request)
    with pytest.raises(ValidationError, match="frozen_instance"):
        result.text = "changed"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelLaneRulesBlockResult.model_validate(
            {"operation": "render_block", "extra": True}
        )


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"operation": "render_block", "block_text": "<omni " + "plugin>/scripts/x"},
            {
                "operation": "render_block",
                "text": '"$OMNI_PLUGIN_ROOT"/scripts/x',
                "path": None,
                "exit_code": 0,
                "message": None,
            },
        ),
        (
            {"operation": "contract_step", "ticket": "TASK-42"},
            {
                "operation": "contract_step",
                "text": "Before the first push, write contracts/TASK-42.yaml with a dod_evidence entry per acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped so the hosted runner can run it; the push is refused without it.",
                "path": None,
                "exit_code": 0,
                "message": None,
            },
        ),
        (
            {"operation": "contract_block", "ticket": "TASK-42"},
            {
                "operation": "contract_block",
                "text": "Required when this lane may open or push a PR in a repository whose CI runs repo-evidence / dod-verify or deploy-gate (read its .github/workflows CI config):\nBefore the first push, write contracts/TASK-42.yaml with a dod_evidence entry per acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped so the hosted runner can run it; the push is refused without it.",
                "path": None,
                "exit_code": 0,
                "message": None,
            },
        ),
        (
            {"operation": "block_path", "checkout_path": "/checkout/project"},
            {
                "operation": "block_path",
                "text": None,
                "path": "/checkout/project/lane_rules/lane-rules-block.md",
                "exit_code": 0,
                "message": None,
            },
        ),
        (
            {
                "operation": "block_path",
                "checkout_path": "/checkout/project",
                "block_exists": False,
            },
            {
                "operation": "block_path",
                "text": None,
                "path": None,
                "exit_code": 2,
                "message": "lane rules block is missing: /checkout/project/lane_rules/lane-rules-block.md",
            },
        ),
    ],
)
async def test_golden_chain_over_bus(
    tmp_path: Path, payload: dict[str, Any], expected: dict[str, Any]
) -> None:
    runtime = await _run(tmp_path, payload)
    result = runtime.handler_result
    assert isinstance(result, ModelLaneRulesBlockResult)
    assert result.model_dump() == expected
    assert (
        ModelLaneRulesBlockResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_bus_fails_without_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {"operation": "render_block", "block_text": 5})
    assert runtime.handler_result is None
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
