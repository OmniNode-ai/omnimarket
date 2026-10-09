# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20680: contract -> bus -> compute -> typed result, plus the refusal chain.

Golden chain: a typed decide_items request goes through the registered contract on the
in-memory event bus (RuntimeLocal) and the typed result comes back with the retired
sweep's decisions. Error chain: a request that names a kind without its inputs, or with
inputs the kind does not take, is refused before the handler runs.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_linear_comments_decision_compute as node_package
from omnimarket.nodes.node_linear_comments_decision_compute.models.model_linear_comments_decision import (
    ModelLinearCommentsDecisionRequest,
    ModelLinearCommentsDecisionResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.linear-comments-decision-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.linear-comments-decision-completed.v1"
FIXTURE = json.loads(
    (ROOT / "tests/fixtures/linear_comments_hourly_parity.json").read_text()
)


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeLinearCommentsDecisionCompute, handler_type)
    for side, model in (
        ("input_model", ModelLinearCommentsDecisionRequest),
        ("output_model", ModelLinearCommentsDecisionResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_linear_comments_decision_compute"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_decides_the_fixture_sweep_over_the_bus(
    tmp_path: Path,
) -> None:
    request = ModelLinearCommentsDecisionRequest.model_validate(
        {"kind": "decide_items", "items": FIXTURE["cases"]["items"]}
    )
    input_path = tmp_path / "request.json"
    input_path.write_text(request.model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    result = runtime.handler_result
    assert isinstance(result, ModelLinearCommentsDecisionResult)
    round_trip = ModelLinearCommentsDecisionResult.model_validate_json(
        result.model_dump_json()
    )
    assert round_trip == result
    assert result.status == "decided"
    selected = list(result.selected_comment_ids or ())
    assert selected == FIXTURE["expected"]["select"]
    assert "collab_unanswered_ask" in selected
    by_id = {d.comment_id: d for d in result.decisions or ()}
    assert by_id["bf9a438b-ca16-4e7a-8c92-5f760f0f2df9"].reason_class == "automation"
    assert by_id["8354b627-f18a-4359-b1e3-3a9a873c7fcc"].draft is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"kind": "decide_items"}, "decide_items requires items"),
        ({"kind": "check_draft", "draft": {}, "facts": "f"}, "requires item"),
        ({"kind": "tally", "held": [], "needing_count": 1}, "requires accepted_count"),
        (
            {"kind": "detect_truncation", "outcome": {}, "facts": "f"},
            "does not take facts",
        ),
        ({"kind": "metrics_row", "receipt": None}, "requires receipt"),
        ({"kind": "no_such_decision"}, "kind"),
        (
            {"kind": "tally", "held": [], "needing_count": -1, "accepted_count": 0},
            "greater",
        ),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelLinearCommentsDecisionRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_does_not_complete(tmp_path: Path) -> None:
    input_path = tmp_path / "bad.json"
    input_path.write_text(json.dumps({"kind": "decide_items"}))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
