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


# OMN-22109: omnibase_infra#5555 (head d8823dd2c059) merged from the merge queue at 2026-11-01T21:36:43Z. The PR watcher
# kept the record OPEN (last read 20:55:18Z), so the throughput tick listed it "green, unlanded" for 16 hours and called
# it a CONTROLLER_DEFECT; the controller's own live read (last-facts.json, tick 6356) had it `merged`.
HEAD_5555 = "d8823dd2c0594e70532a4ec7f0efcf130a93bf29"
HEADS_5555 = {"omnibase_infra#5555": HEAD_5555}
MERGED_5555 = {
    "pr": "OmniNode-ai/omnibase_infra#5555",
    "head_sha": HEAD_5555,
    "state": "merged",
    "merge_state": "unknown",
    "ci": "green",
    "red_checks": [],
    "gate_reasons": ["lab_unproven"],
    "suspensions": ["gate"],
    "merge_queue": True,
}


def read_5555(entry: Any) -> frozenset[str]:
    return read(facts(entry), heads=HEADS_5555)


@pytest.mark.parametrize("state", ["merged", "closed", "MERGED", "Closed"])
def test_a_head_the_controller_reads_merged_or_closed_is_not_unlanded(
    state: str,
) -> None:
    assert read_5555({**MERGED_5555, "state": state}) == {"omnibase_infra#5555"}


@pytest.mark.parametrize("state", ["open", "OPEN", "", None])
def test_a_head_the_controller_reads_open_and_green_stays_green(
    state: str | None,
) -> None:
    assert read_5555({**MERGED_5555, "state": state}) == frozenset()


def test_a_merged_read_at_another_head_than_the_watchers_is_not_applied() -> None:
    assert read_5555({**MERGED_5555, "head_sha": "f" * 40}) == frozenset()


def test_the_recorded_5555_case_is_read_the_same_on_both_profiles(
    tmp_path: Path,
) -> None:
    """Two profiles: the handler called in process on the facts document, and the node entry reading it from the
    controller's state directory (last-facts.json). Both give the same answer for the recorded case and for the open one."""
    for entry, expected in (
        (MERGED_5555, {"omnibase_infra#5555"}),
        ({**MERGED_5555, "state": "open"}, set()),
    ):
        doc = facts(entry)
        (tmp_path / "last-facts.json").write_text(json.dumps(doc))
        in_process = set(read(doc, heads=HEADS_5555))
        through_node = node.controller_not_green(
            tmp_path, HEADS_5555, now=NOW, bound_min=30
        )
        assert in_process == through_node == expected
