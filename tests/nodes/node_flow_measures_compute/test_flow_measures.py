# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_flow_measures_compute: the five measures, `unread`, the window and the bindings (OMN-21441)."""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_flow_measures_compute import NodeFlowMeasuresCompute
from omnimarket.nodes.node_flow_measures_compute.handlers.handler_flow_measures import (
    CONTRACT_PATH,
    HandlerFlowMeasures,
    load_flow_measures_config,
)
from omnimarket.nodes.node_flow_measures_compute.models.model_flow_measures import (
    EnumFillEvent,
    EnumHoldReason,
    ModelFillEventFact,
    ModelFillSource,
    ModelFlowMeasure,
    ModelFlowMeasuresRequest,
    ModelFlowMeasuresResult,
    ModelHeldFact,
    ModelHeldSource,
    ModelMergeFact,
    ModelMergeSource,
    ModelStopRecord,
    ModelStopSource,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
READ = NOW - timedelta(minutes=3)
NODE_DIR = CONTRACT_PATH.parent
ROOT = NODE_DIR.parents[3]


def _merge(repo: str, days_ago: float, green_minutes: float | None) -> ModelMergeFact:
    merged = NOW - timedelta(days=days_ago)
    green = None if green_minutes is None else merged - timedelta(minutes=green_minutes)
    return ModelMergeFact(repository=repo, merged_at=merged, green_at=green)


def _request(**over: Any) -> ModelFlowMeasuresRequest:
    facts: dict[str, Any] = {
        "now": NOW,
        "repositories": ("repo-a", "repo-b", "repo-c"),
        "merges": ModelMergeSource(
            read_at=READ,
            basis="merge log",
            repositories_read=("repo-a", "repo-b"),
            merges=(
                _merge("repo-a", 1, 30),
                _merge("repo-a", 2, 120),
                _merge("repo-a", 5, 60),
                _merge("repo-a", 9, 45),
            ),
        ),
        "held": ModelHeldSource(
            read_at=READ,
            basis="open PR list",
            repositories_read=("repo-a", "repo-b"),
            held=(
                ModelHeldFact(
                    repository="repo-a", reason=EnumHoldReason.WIP_OVER_LIMIT
                ),
                ModelHeldFact(
                    repository="repo-a", reason=EnumHoldReason.WIP_OVER_LIMIT
                ),
                ModelHeldFact(
                    repository="repo-a", reason=EnumHoldReason.HELD_FOR_PERSON
                ),
            ),
        ),
        "stops": ModelStopSource(
            read_at=READ,
            basis="stop records",
            records=(
                ModelStopRecord(
                    detected_at=NOW - timedelta(days=1),
                    stopped_at=NOW - timedelta(days=1) + timedelta(minutes=10),
                    fixed_at=NOW - timedelta(days=1) + timedelta(minutes=40),
                ),
                ModelStopRecord(
                    detected_at=NOW - timedelta(days=2),
                    stopped_at=NOW - timedelta(days=2) + timedelta(minutes=20),
                    fixed_at=NOW - timedelta(days=2) + timedelta(minutes=100),
                ),
                ModelStopRecord(
                    detected_at=NOW - timedelta(days=10),
                    stopped_at=NOW - timedelta(days=10) + timedelta(minutes=500),
                ),
            ),
        ),
        "fill": ModelFillSource(
            read_at=READ,
            basis="fill journal",
            repositories_read=("repo-a",),
            events=(
                ModelFillEventFact(
                    repository="repo-a",
                    event=EnumFillEvent.WIP_DIVERTED,
                    at=NOW - timedelta(days=1),
                ),
                ModelFillEventFact(
                    repository="repo-a",
                    event=EnumFillEvent.WIP_DIVERTED,
                    at=NOW - timedelta(days=4),
                ),
                ModelFillEventFact(
                    repository="repo-a",
                    event=EnumFillEvent.WIP_DIVERTED,
                    at=NOW - timedelta(days=12),
                ),
                ModelFillEventFact(
                    repository="repo-a",
                    event=EnumFillEvent.WIP_IDLE_OVERRIDE,
                    at=NOW - timedelta(days=2),
                ),
            ),
        ),
    }
    facts.update(over)
    return ModelFlowMeasuresRequest(**facts)


def _window_contract(tmp_path: Path, days: int) -> Path:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["config"]["wip_gate"]["trailing_days"] = days
    path = tmp_path / f"contract_{days}d.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


def _by(result: ModelFlowMeasuresResult, label: str, subject: str) -> ModelFlowMeasure:
    (found,) = (m for m in result.measures if m.label == label and m.subject == subject)
    return found


def test_each_of_the_five_measures_prints_with_its_read_time_and_basis() -> None:
    result = HandlerFlowMeasures().handle(_request())
    assert result.trailing_days == 7

    rate = _by(result, "merges_per_hour", "repo-a")
    assert rate.value == pytest.approx(3 / 168)
    assert rate.display == "0.018/h"
    assert rate.read_at == READ
    assert "merge log" in rate.basis
    assert rate.line.startswith(
        "merges_per_hour repo-a: 0.018/h | read 2026-10-27T11:57:00Z"
    )

    median = _by(result, "green_to_merged_median", "repo-a")
    assert median.value == 60 * 60
    assert median.display == "60.0 min"
    assert "median of 3 merges" in median.basis

    assert _by(result, "wip_over_limit", "repo-a").display == "2"
    assert _by(result, "held_for_person", "repo-a").display == "1"
    assert _by(result, "held_by_controller", "repo-a").display == "0"
    assert "open PR list" in _by(result, "wip_over_limit", "repo-a").basis

    assert _by(result, "detection_to_stop", "all").display == "15.0 min"
    assert _by(result, "stop_to_fix", "all").display == "55.0 min"
    assert "stop records" in _by(result, "stop_to_fix", "all").basis

    assert _by(result, "wip_diverted", "repo-a").display == "2"
    assert _by(result, "wip_idle_override", "repo-a").display == "1"
    assert "fill journal" in _by(result, "wip_diverted", "repo-a").basis

    assert len(result.lines) == len(result.measures)
    assert all("| read " in line and "| basis " in line for line in result.lines)


def test_a_repository_with_no_data_prints_unread_not_zero() -> None:
    result = HandlerFlowMeasures().handle(_request())
    for label in (
        "merges_per_hour",
        "green_to_merged_median",
        "wip_over_limit",
        "held_for_person",
        "held_by_controller",
        "wip_diverted",
        "wip_idle_override",
    ):
        unread = _by(result, label, "repo-c")
        assert unread.value is None
        assert unread.display == "unread"
        assert unread.read_at is None
        assert unread.line.endswith(" unread | read never | basis repository not read")
    # repo-b was read by the merge and held sources but not by the fill
    assert _by(result, "wip_diverted", "repo-b").display == "unread"
    # positive control: a repository that was read and has nothing to count prints a real zero
    assert _by(result, "merges_per_hour", "repo-b").display == "0.000/h"
    assert _by(result, "wip_over_limit", "repo-b").display == "0"
    # a read repository with merges but no green time has no median to print
    assert _by(result, "green_to_merged_median", "repo-b").display == "unread"


def test_a_source_nobody_read_prints_unread_for_every_subject() -> None:
    result = HandlerFlowMeasures().handle(
        _request(merges=None, held=None, stops=None, fill=None)
    )
    assert {m.display for m in result.measures} == {"unread"}
    assert all(m.read_at is None for m in result.measures)
    assert all("read never" in m.line for m in result.measures)


def test_the_trailing_window_is_config_wip_gate_trailing_days(tmp_path: Path) -> None:
    request = _request()
    seven = HandlerFlowMeasures(
        load_flow_measures_config(_window_contract(tmp_path, 7))
    )
    three = HandlerFlowMeasures(
        load_flow_measures_config(_window_contract(tmp_path, 3))
    )
    wide = _by(seven.handle(request), "merges_per_hour", "repo-a")
    narrow = _by(three.handle(request), "merges_per_hour", "repo-a")
    assert wide.value == pytest.approx(3 / (7 * 24))
    assert narrow.value == pytest.approx(2 / (3 * 24))
    assert wide.value != narrow.value
    assert _by(three.handle(request), "green_to_merged_median", "repo-a").value == (
        75 * 60
    )
    assert _by(three.handle(request), "wip_diverted", "repo-a").display == "1"


def test_the_default_window_is_read_from_the_contract_through_the_loader() -> None:
    declared = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert load_flow_measures_config().trailing_days == 7
    assert (
        load_flow_measures_config().trailing_days
        == declared["config"]["wip_gate"]["trailing_days"]
    )
    assert HandlerFlowMeasures().handle(_request()).trailing_days == 7


def test_a_contract_without_the_window_fails_closed(tmp_path: Path) -> None:
    bare = tmp_path / "contract.yaml"
    bare.write_text("config: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"config\.wip_gate is missing"):
        load_flow_measures_config(bare)


def test_a_row_for_a_repository_the_source_did_not_read_is_refused() -> None:
    with pytest.raises(ValueError, match="unread repositories"):
        ModelMergeSource(
            read_at=READ,
            basis="merge log",
            repositories_read=("repo-a",),
            merges=(_merge("repo-b", 1, 10),),
        )


def _protocol_dependency_paths(node: object, trail: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else str(key)
            if key == "protocol_dependencies":
                found.append(here)
            found.extend(_protocol_dependency_paths(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_protocol_dependency_paths(value, f"{trail}[{index}]"))
    return found


def test_the_contract_declares_no_protocol_dependencies() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert _protocol_dependency_paths(contract) == []
    # positive control: the scan finds an entry when one is added
    contract["protocol_dependencies"] = [{"name": "any"}]
    assert _protocol_dependency_paths(contract) == ["protocol_dependencies"]


def test_results_are_byte_identical_in_process_and_through_the_node_entry() -> None:
    request = _request()
    in_process = HandlerFlowMeasures().handle(request).model_dump_json()
    assert HandlerFlowMeasures().handle(request).model_dump_json() == in_process
    assert NodeFlowMeasuresCompute().handle(request).model_dump_json() == in_process
    round_tripped = ModelFlowMeasuresRequest.model_validate_json(
        request.model_dump_json()
    )
    assert HandlerFlowMeasures().handle(round_tripped).model_dump_json() == in_process


def test_the_node_names_no_host_path_repository_or_organization() -> None:
    forbidden = re.compile(
        r"192\.168\.|/(?:Users|Volumes)/|://|OmniNode-ai|omnimarket\.ai|\.git\b"
    )
    sources = [
        p for p in NODE_DIR.rglob("*") if p.suffix in {".py", ".yaml"} and p.is_file()
    ]
    assert len(sources) >= 7
    assert [p.name for p in sources if forbidden.search(p.read_text("utf-8"))] == []


def test_the_node_is_a_registered_entry_point_with_matching_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry = pyproject["project"]["entry-points"]["onex.nodes"]
    assert (
        entry["node_flow_measures_compute"]
        == "omnimarket.nodes.node_flow_measures_compute"
    )
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text("utf-8"))
    assert metadata["entry_points"]["onex.nodes"]["node_flow_measures_compute"] == (
        "omnimarket.nodes.node_flow_measures_compute"
    )
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"


def test_the_contract_routes_one_command_to_one_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.flow-measures-compute-requested.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.flow-measures-computed.v1"
    ]
    assert contract["terminal_event"] == "onex.evt.omnimarket.flow-measures-computed.v1"
    assert contract["handler_routing"]["handlers"][0]["operation"] == (
        "compute_flow_measures"
    )
