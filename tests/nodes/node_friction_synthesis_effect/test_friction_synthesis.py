# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enumerated source handoff regression fixtures."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def harvest(
    anchors: list[int], count: object = None, source: str = "ledger"
) -> dict[str, Any]:
    return {
        "source_id": "checkpoints",
        "verdict": "READ",
        "candidates": [
            {
                "fingerprint": "fr-tick-runid-unrecorded",
                "signature": "tick omitted its run id",
                "occurrences_this_run": len(anchors) if count is None else count,
                "evidence": [
                    {"source": source, "anchor": f"L{n}", "quote": "completion"}
                    for n in anchors
                ],
                "why_friction": "completion cannot be traced",
            }
        ],
    }


def merge(handoffs: list[dict[str, Any]]) -> dict[str, Any]:
    from omnimarket.nodes.node_friction_synthesis_effect import (
        merge_handoffs,
    )

    return merge_handoffs(handoffs)


@pytest.mark.unit
def test_overclaimed_16_with_4_anchors_is_four_and_unproven() -> None:
    result = merge([harvest([1, 2, 3, 4], count=16)])
    assert result["candidates"][0]["occurrences_this_run"] == 4
    assert (
        result["candidates"][0]["evidence"]
        == harvest([1, 2, 3, 4])["candidates"][0]["evidence"]
    )
    note = result["unproven"][0]
    assert note["verdict"] == "UNPROVEN"
    assert note["asserted_count"] == 16
    assert note["enumerated_count"] == 4
    assert note["unsupported_contribution"] == 0


@pytest.mark.unit
def test_two_harvests_share_three_of_five_anchors_merge_to_seven() -> None:
    result = merge([harvest([1, 2, 3, 4, 5]), harvest([3, 4, 5, 6, 7])])
    assert result["candidates"][0]["occurrences_this_run"] == 7
    assert len(result["candidates"][0]["evidence"]) == 7
    assert result["unproven"] == []


@pytest.mark.unit
def test_same_anchor_in_different_sources_is_distinct() -> None:
    result = merge([harvest([1], source="ledger"), harvest([1], source="checkpoint")])
    assert result["candidates"][0]["occurrences_this_run"] == 2


@pytest.mark.unit
def test_replay_and_duplicates_within_harvest_do_not_inflate() -> None:
    data = harvest([1, 1, 2])
    result = merge([data, data])
    assert result["candidates"][0]["occurrences_this_run"] == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [{"source": "ledger"}, {"anchor": "L2"}, {"source": "", "anchor": "L3"}, "L4"],
)
def test_missing_source_or_anchor_is_unproven(bad: object) -> None:
    data = harvest([1], count=2)
    data["candidates"][0]["evidence"].append(bad)
    result = merge([data])
    assert result["candidates"][0]["occurrences_this_run"] == 1
    assert result["unproven"]


@pytest.mark.unit
@pytest.mark.parametrize("count", [True, -1, "16", 0, 2])
def test_invalid_or_underclaimed_count_is_reported(count: object) -> None:
    result = merge([harvest([1, 2, 3], count=count)])
    assert result["candidates"][0]["occurrences_this_run"] == 3
    assert result["unproven"]


@pytest.mark.unit
def test_unknown_source_cannot_contribute_even_if_it_has_candidates() -> None:
    data = harvest([1, 2, 3])
    data.update(verdict="UNKNOWN", freshness="264h against 48h")
    result = merge([data])
    assert result["candidates"] == []
    assert "checkpoints" in result["sources_unknown"][0]


@pytest.mark.unit
def test_unanchored_claim_contributes_zero() -> None:
    result = merge([harvest([], count=16)])
    assert result["candidates"][0]["occurrences_this_run"] == 0
    assert result["unproven"][0]["unsupported_contribution"] == 0


@pytest.mark.unit
def test_cli_counts_and_verifies_before_adjudication(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "merged.json"
    source.write_text(json.dumps(harvest([1, 2, 3, 4], count=16)))
    command = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_friction_synthesis_effect.handlers.handler_friction_synthesis_cli",
        str(source),
    ]
    counted = subprocess.run(
        [*command, "--output", str(output)], capture_output=True, text=True
    )
    assert counted.returncode == 0, counted.stderr
    assert "UNPROVEN" in counted.stderr
    result = json.loads(output.read_text())
    result.update(root_causes=["tick tracing"], pickups=[])
    output.write_text(json.dumps(result))
    checked = subprocess.run(
        [*command, "--verify", str(output)], capture_output=True, text=True
    )
    assert checked.returncode == 0, checked.stderr
    result["candidates"][0]["occurrences_this_run"] = 16
    output.write_text(json.dumps(result))
    rejected = subprocess.run(
        [*command, "--verify", str(output)], capture_output=True, text=True
    )
    assert rejected.returncode != 0
    assert "UNPROVEN" in rejected.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "field", ["evidence", "sources", "unproven", "sources_unknown", "candidates"]
)
def test_verification_refuses_accounting_field_edits(field: str) -> None:
    from omnimarket.nodes.node_friction_synthesis_effect import (
        verify_accounting,
    )

    expected = merge([harvest([1], count=16)])
    actual = json.loads(json.dumps(expected))
    if field in ("evidence", "sources"):
        actual["candidates"][0][field] = []
    elif field == "sources_unknown":
        actual[field] = ["fabricated source"]
    else:
        actual[field] = []
    with pytest.raises(ValueError, match="UNPROVEN"):
        verify_accounting(expected, actual)


def test_canonical_runtime_counts_and_refuses_tampering(tmp_path: Path) -> None:
    from omnimarket.nodes.node_friction_synthesis_effect import (
        ModelFrictionSynthesisRequest,
    )
    from omnimarket.nodes.node_friction_synthesis_effect.handlers import (
        handler_friction_synthesis_cli as cli,
    )

    source = tmp_path / "source.json"
    output = tmp_path / "merged.json"
    source.write_text(json.dumps(harvest([1, 2, 3, 4], count=16)))
    request = ModelFrictionSynthesisRequest(handoffs=[source], output=output)
    result = cli.dispatch(request)
    assert result.correlation_id == request.correlation_id
    assert result.exit_code == 0
    assert "UNPROVEN" in result.stderr
    observed = json.loads(output.read_text())
    assert observed["candidates"][0]["occurrences_this_run"] == 4
    verify = ModelFrictionSynthesisRequest(handoffs=[source], verify=output)
    assert cli.dispatch(verify).exit_code == 0
    observed["candidates"][0]["occurrences_this_run"] = 16
    output.write_text(json.dumps(observed))
    rejected = cli.dispatch(verify)
    assert rejected.exit_code == 1
    assert "UNPROVEN" in rejected.stderr


def test_synthesis_node_registered_and_topics_routed() -> None:
    import importlib
    from importlib.metadata import entry_points

    import yaml
    from omnibase_core.nodes.node_effect import NodeEffect

    entry = next(
        iter(entry_points(group="onex.nodes", name="node_friction_synthesis_effect"))
    )
    node = importlib.import_module(f"{entry.value}.node").NodeFrictionSynthesisEffect
    assert issubclass(node, NodeEffect)
    assert "process" not in node.__dict__
    root = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_friction_synthesis_effect"
    )
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert not (root.parents[1] / "handlers/handler_friction_synthesis.py").exists()
