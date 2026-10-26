"""The controller's not-green read used by the throughput tick's green-unlanded alarm (OMN-19998)."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE = "omnimarket.nodes.node_green_controller_read_compute"
handler = importlib.import_module(f"{NODE}.handler")
models = importlib.import_module(f"{NODE}.models")
node = importlib.import_module(f"{NODE}.node")
NOW = datetime(2026, 10, 19, 4, 33, tzinfo=UTC)
HEAD_979 = "e7bac5a5f13a014f846173dbea475c529120be6f"
HEAD_4138 = "248f3402a6105b6fd4a52275bb814484b7fe5dcb"
HEADS = {"plugin-repo-a#979": HEAD_979, "omnimarket#4138": HEAD_4138}


def stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def facts(*entries: Any, minutes_ago: float = 6) -> dict[str, Any]:
    return {"observed_at": stamp(minutes_ago), "prs": list(entries)}


CONFLICT_979 = {
    "pr": "OmniNode-ai/plugin-repo-a#979",
    "head_sha": HEAD_979,
    "merge_state": "conflicting",
    "ci": "green",
    "red_checks": [],
}
ABSENT_4138 = {
    "pr": "OmniNode-ai/omnimarket#4138",
    "head_sha": HEAD_4138,
    "merge_state": "blocked",
    "ci": "red",
    "red_checks": [
        "CI Summary",
        "Customer Path Boundary (OMN-18012)",
        "JS Toolchain Hermetic (OMN-18012)",
        "Wire Compatibility Gate",
    ],
}


def read(
    doc: Any,
    heads: dict[str, str] = HEADS,
    now: datetime | None = NOW,
    bound: float | None = 30,
) -> frozenset[str]:
    request = models.ModelGreenControllerReadRequest(doc, heads, now, bound)
    return handler.HandlerGreenControllerRead().handle(request).not_green


def test_a_conflicting_and_a_red_head_at_the_watchers_head_are_not_green() -> None:
    assert read(facts(CONFLICT_979, ABSENT_4138)) == {
        "plugin-repo-a#979",
        "omnimarket#4138",
    }


@pytest.mark.parametrize(
    "entry",
    [
        {**CONFLICT_979, "merge_state": "clean"},
        {**CONFLICT_979, "merge_state": "unknown"},
        {**ABSENT_4138, "red_checks": []},
        {**ABSENT_4138, "ci": "pending"},
        {**CONFLICT_979, "head_sha": "f" * 40},
        {**ABSENT_4138, "head_sha": "f" * 40},
        {**CONFLICT_979, "pr": "OmniNode-ai/plugin-repo-a#1002"},
        "not a mapping",
    ],
)
def test_a_head_the_controller_did_not_read_not_green_here_stays_green(
    entry: Any,
) -> None:
    assert read(facts(entry)) == frozenset()


@pytest.mark.parametrize(
    "doc",
    [
        None,
        {},
        {"prs": "x"},
        facts(CONFLICT_979, minutes_ago=31),
        {"prs": [CONFLICT_979]},
        {"observed_at": "invalid", "prs": [CONFLICT_979]},
    ],
)
def test_stale_unstamped_or_malformed_facts_read_nothing(doc: Any) -> None:
    assert read(doc) == frozenset()


def test_no_bound_reads_facts_of_any_age() -> None:
    assert read(facts(CONFLICT_979, minutes_ago=600), now=None, bound=None) == {
        "plugin-repo-a#979"
    }


def test_the_node_reads_last_facts_json_and_an_unreadable_file_reads_nothing(
    tmp_path: Path,
) -> None:
    assert node.controller_not_green(tmp_path, HEADS, now=NOW, bound_min=30) == set()
    (tmp_path / "last-facts.json").write_text("{not json")
    assert node.controller_not_green(tmp_path, HEADS, now=NOW, bound_min=30) == set()
    (tmp_path / "last-facts.json").write_text(
        json.dumps(facts(CONFLICT_979, ABSENT_4138))
    )
    assert node.controller_not_green(tmp_path, HEADS, now=NOW, bound_min=30) == {
        "plugin-repo-a#979",
        "omnimarket#4138",
    }


def test_the_contract_names_the_handler_models_and_entrypoint() -> None:
    contract = yaml.safe_load(node.CONTRACT.read_text())
    assert contract["name"] == "node_green_controller_read_compute"
    assert contract["node_type"] == "compute"
    assert contract["handler"]["class"] == "HandlerGreenControllerRead"
    assert contract["entrypoint"]["name"] == "NodeGreenControllerReadCompute"
    assert contract["input_model"]["name"] == "ModelGreenControllerReadRequest"
    assert contract["output_model"]["name"] == "ModelGreenControllerReadResult"
