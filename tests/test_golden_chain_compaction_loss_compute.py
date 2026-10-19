# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for compaction loss compute (OMN-21122)."""

from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_loss_compute.handlers import (
    HandlerCompactionLoss,
    HandlerCompactionPairs,
)
from omnimarket.nodes.node_compaction_loss_compute.models import (
    EnumMustCarryKind,
    ModelCompactionLossRequest,
    ModelCompactionPairsRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
NODE_NAME = "node_compaction_loss_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME
COMMAND_TOPIC = "onex.cmd.omnimarket.compaction-loss-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.compaction-loss-measured.v1"

BOUNDARY = "b0000000-0000-0000-0000-000000000001"


def _user(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": list(blocks)},
    }


def _transcript() -> list[dict[str, Any]]:
    claim = "2026-10-19T07:00:00Z | CLAIM | lane=alpha-1500 | ticket=OMN-21122 | est ~1 lane-hours"
    return [
        _user("old turn before an earlier compaction: OMN-11111"),
        {"type": "system", "subtype": "compact_boundary", "uuid": "b-old"},
        _user("Earlier summary carried OMN-22222.", isCompactSummary=True),
        _user(
            "Keep the foreground silent. Per the ruling 2026-10-14T21:46:25Z, land "
            "omnimarket#4184 and track OMN-21122."
        ),
        _assistant(
            {"type": "thinking", "thinking": "OMN-99999 is private reasoning"},
            {
                "type": "text",
                "text": "I will report the dead-man results to the orchestrator.",
            },
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": f"onex-ledger --append '{claim}'"},
            },
        ),
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "OMN-33333 in a tool result"}
                ],
            },
        },
        _user("<local-command-caveat>meta</local-command-caveat>", isMeta=True),
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": BOUNDARY,
            "sessionId": "sess-1",
            "compactMetadata": {
                "trigger": "auto",
                "preTokens": 9000,
                "postTokens": 900,
            },
        },
        _user(
            "Summary: tracking OMN-21122 and omnimarket#4184 under lane alpha-1500.",
            isCompactSummary=True,
        ),
        _assistant({"type": "text", "text": "Continuing."}),
    ]


def test_golden_chain_pairs_then_scores_loss() -> None:
    """The newest boundary pairs with its window; identifiers survive, prose drops."""
    pairs = HandlerCompactionPairs().handle(
        ModelCompactionPairsRequest(session_id="sess-1", records=tuple(_transcript()))
    )
    assert [p.compaction_id for p in pairs.pairs] == ["b-old", BOUNDARY]
    pair = pairs.pairs[-1]
    assert pair.trigger == "auto"
    assert pair.pre_tokens == 9000
    assert pair.post_summary.startswith("Summary: tracking OMN-21122")
    # The earlier summary is carried context of this window; the pre-boundary turn is not.
    assert "OMN-22222" in pair.pre_text
    assert "OMN-11111" not in pair.pre_text
    # Tool results, thinking and meta records are not the conversation's own words.
    assert "OMN-33333" not in pair.pre_text
    assert "OMN-99999" not in pair.pre_text

    result = HandlerCompactionLoss().handle(pair)
    by_key = {(item.kind, item.key): item for item in result.items}
    assert by_key[(EnumMustCarryKind.TICKET, "OMN-21122")].survived
    assert not by_key[(EnumMustCarryKind.TICKET, "OMN-22222")].survived
    assert by_key[(EnumMustCarryKind.PR, "omnimarket#4184")].survived
    assert by_key[(EnumMustCarryKind.LANE_CLAIM, "alpha-1500")].survived
    assert not by_key[(EnumMustCarryKind.RULING, "2026-10-14T21:46:25Z")].survived
    assert not by_key[(EnumMustCarryKind.OPERATOR_MODE, "silent")].survived
    promises = [i for i in result.items if i.kind is EnumMustCarryKind.PROMISE]
    assert len(promises) == 1
    assert not promises[0].survived
    instructions = [
        i for i in result.items if i.kind is EnumMustCarryKind.OPERATOR_INSTRUCTION
    ]
    assert instructions
    assert not any(i.survived for i in instructions)

    recall = {r.kind: r for r in result.recall_by_kind}
    assert recall[EnumMustCarryKind.TICKET].total == 2
    assert recall[EnumMustCarryKind.TICKET].survived == 1
    assert recall[EnumMustCarryKind.TICKET].recall == pytest.approx(0.5)
    assert {i.key for i in result.dropped} >= {
        "OMN-22222",
        "silent",
        "2026-10-14T21:46:25Z",
    }
    assert all(not i.survived for i in result.dropped)
    assert result.items_total == len(result.items)
    assert result.items_survived == sum(1 for i in result.items if i.survived)

    # The eval case uses the presence-case field names, so a pack is judged on the same items.
    case = result.eval_case
    assert case.case_id == f"sess-1:{BOUNDARY}"
    assert case.candidate_id == "stock-summary"
    assert case.package_text == pair.post_summary
    assert len(case.expected) == result.items_total
    assert all(e.required_spans for e in case.expected)


def test_identical_summary_scores_full_recall() -> None:
    """Positive control: a summary equal to the window keeps every item."""
    text = "Keep the foreground silent. Track OMN-1 and omniclaude#2876. I will report back."
    result = HandlerCompactionLoss().handle(
        ModelCompactionLossRequest(
            session_id="s",
            compaction_id="c",
            pre_text=text,
            pre_user_text=text,
            pre_assistant_text=text,
            post_summary=text,
        )
    )
    assert result.items_total >= 4
    assert result.items_survived == result.items_total
    assert result.overall_recall == pytest.approx(1.0)
    assert result.dropped == ()


def test_paraphrased_promise_survives_on_stems_not_filler_words() -> None:
    """A summary that restates a promise in other inflections keeps it."""
    promise = "I will run the 27-shot triage manually against any queue that becomes reachable."
    result = HandlerCompactionLoss().handle(
        ModelCompactionLossRequest(
            session_id="s",
            compaction_id="c",
            pre_text=promise,
            pre_assistant_text=promise,
            post_summary="Owed: 27-shot manual triage once a queue is reachable.",
        )
    )
    (item,) = [i for i in result.items if i.kind is EnumMustCarryKind.PROMISE]
    assert "against" not in item.required_spans
    assert item.survived


def test_empty_window_has_no_items_and_null_recall() -> None:
    result = HandlerCompactionLoss().handle(
        ModelCompactionLossRequest(session_id="s", compaction_id="c", post_summary="x")
    )
    assert result.items == ()
    assert result.overall_recall is None


def test_no_boundary_yields_no_pairs() -> None:
    pairs = HandlerCompactionPairs().handle(
        ModelCompactionPairsRequest(session_id="s", records=(_user("hello"),))
    )
    assert pairs.pairs == ()


def test_error_chain_malformed_request_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCompactionLossRequest.model_validate({"session_id": "s"})
    with pytest.raises(ValidationError):
        ModelCompactionLossRequest.model_validate(
            {"session_id": "s", "compaction_id": "c", "post_summary": "x", "extra": 1}
        )


def test_contract_loads_and_declares_its_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == "compaction_loss_compute"
    assert contract["node_type"] == "compute"
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert contract["terminal_event"] == COMPLETED_TOPIC
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["externally_consumed_topics"] == [COMPLETED_TOPIC]
    assert contract["metadata"] == {"ticket": "OMN-21122", "transport_type": "kafka"}
    assert contract["descriptor"]["purity"] == "pure"
    routes = contract["handler_routing"]["handlers"]
    assert [r["operation"] for r in routes] == [
        "pair_compactions",
        "measure_compaction_loss",
    ]
    for route in routes:
        module = importlib.import_module(route["handler"]["module"])
        assert hasattr(module, route["handler"]["name"])
        for key in ("input_model", "output_model"):
            mod_name, _, cls_name = route[key].rpartition(".")
            assert hasattr(importlib.import_module(mod_name), cls_name)


def test_entry_point_registered_and_loads() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE
    metadata = yaml.safe_load((NODE / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == NODE_NAME
    assert metadata["entry_points"]["onex.nodes"][NODE_NAME] == PACKAGE
    matches = [ep for ep in entry_points(group="onex.nodes") if ep.name == NODE_NAME]
    if matches:
        assert matches[0].value == PACKAGE
