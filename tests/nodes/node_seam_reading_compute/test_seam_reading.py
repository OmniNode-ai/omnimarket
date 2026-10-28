# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_reading_compute: lines, thresholds from the contract, one home for the verdict word (OMN-21459)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.seam_reading import (
    EnumSeamCrossingState,
    ModelSeamCrossing,
    ModelSeamReading,
    ModelSeamWindow,
)
from omnimarket.nodes.node_seam_reading_compute import NodeSeamReadingCompute
from omnimarket.nodes.node_seam_reading_compute.handlers.handler_seam_reading import (
    ORCHESTRATOR_CONTRACT_PATH,
    HandlerSeamReading,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_seam_reading_request import (
    ModelSeamReadingRequest,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
WINDOW = ModelSeamWindow(start=NOW - timedelta(hours=24), end=NOW)
SRC = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
FIRST_SEAM_NODE = "sm_00_backlog_to_fill"


def _crossings(
    matched: int, mismatched: int = 0, unreadable: int = 0
) -> list[ModelSeamCrossing]:
    out = [
        ModelSeamCrossing(key=f"ok-{i}", state=EnumSeamCrossingState.MATCHED)
        for i in range(matched)
    ]
    out += [
        ModelSeamCrossing(
            key=f"bad-{i}",
            state=EnumSeamCrossingState.MISMATCHED,
            reason="engine differs",
        )
        for i in range(mismatched)
    ]
    out += [
        ModelSeamCrossing(
            key=f"unread-{i}", state=EnumSeamCrossingState.UNREADABLE, reason="no row"
        )
        for i in range(unreadable)
    ]
    return out


def _reading(seam_id: str, **counts: int) -> ModelSeamReading:
    return ModelSeamReading.from_crossings(seam_id, WINDOW, _crossings(**counts), NOW)


def _fixture_contract(
    tmp_path: Path, minimum: int = 20, expected: int | None = 100
) -> Path:
    raw = yaml.safe_load(ORCHESTRATOR_CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["config"]["seam_read"]["default_minimum_crossings"] = minimum
    for node in raw["workflow_coordination"]["workflow_definition"]["execution_graph"][
        "nodes"
    ]:
        step = node.get("step_config")
        if step is not None and expected is not None:
            step["expected_crossings"] = expected
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _render(contract: Path, *readings: ModelSeamReading) -> tuple[str, ...]:
    return (
        HandlerSeamReading(contract)
        .handle(ModelSeamReadingRequest(readings=readings, now=NOW))
        .lines
    )


def _lines_for(lines: tuple[str, ...], seam_id: str) -> list[str]:
    return [line for line in lines if f" {seam_id} " in line or f":{seam_id} " in line]


def test_a_reading_with_mismatched_two_renders_a_named_line(tmp_path: Path) -> None:
    lines = _render(
        _fixture_contract(tmp_path), _reading("SM-02", matched=30, mismatched=2)
    )
    mine = _lines_for(lines, "SM-02")
    assert any(line.startswith("MISSING seam:SM-02 ") for line in mine)
    assert any("mismatched=2" in line for line in mine)
    assert any("examples=bad-0,bad-1" in line for line in mine)


def test_zero_crossings_and_three_unreadable_reads_unknown_never_ok(
    tmp_path: Path,
) -> None:
    lines = _render(
        _fixture_contract(tmp_path), _reading("SM-02", matched=0, unreadable=3)
    )
    mine = _lines_for(lines, "SM-02")
    assert any(line.startswith("UNKNOWN seam:SM-02 ") for line in mine)
    assert not any("status=OK" in line for line in mine)


def test_an_empty_window_reads_unknown(tmp_path: Path) -> None:
    lines = _render(_fixture_contract(tmp_path), _reading("SM-02", matched=0))
    mine = _lines_for(lines, "SM-02")
    assert "UNKNOWN seam:SM-02 reason=empty-window" in mine
    assert not any("status=OK" in line for line in mine)


def test_a_partly_unreadable_window_reads_unknown(tmp_path: Path) -> None:
    lines = _render(
        _fixture_contract(tmp_path), _reading("SM-02", matched=50, unreadable=1)
    )
    mine = _lines_for(lines, "SM-02")
    assert any(line.startswith("UNKNOWN seam:SM-02 ") for line in mine)
    assert not any("status=OK" in line for line in mine)


def test_a_window_under_its_minimum_never_reads_ok(tmp_path: Path) -> None:
    lines = _render(_fixture_contract(tmp_path), _reading("SM-02", matched=19))
    mine = _lines_for(lines, "SM-02")
    assert any(
        line.startswith("UNKNOWN seam:SM-02 reason=below-minimum") for line in mine
    )
    assert not any("status=OK" in line for line in mine)


def test_the_minimum_is_read_from_the_contract(tmp_path: Path) -> None:
    contract = _fixture_contract(tmp_path, minimum=5)
    six = _lines_for(_render(contract, _reading("SM-02", matched=6)), "SM-02")
    four = _lines_for(_render(contract, _reading("SM-02", matched=4)), "SM-02")
    assert six == [
        "SEAM SM-02 crossings=6 mismatched=0 unreadable=0 window=24h status=OK"
    ]
    assert any(line.startswith("UNKNOWN seam:SM-02 ") for line in four)


def test_a_seam_node_without_expected_crossings_refuses_naming_the_key(
    tmp_path: Path,
) -> None:
    contract = _fixture_contract(tmp_path, expected=None)
    with pytest.raises(ValueError, match="expected_crossings"):
        _render(contract, _reading("SM-02", matched=30))


def test_the_committed_contract_refuses_until_expected_crossings_are_set() -> None:
    with pytest.raises(ValueError, match="expected_crossings"):
        HandlerSeamReading().handle(ModelSeamReadingRequest(readings=(), now=NOW))


def test_a_missing_config_key_refuses_naming_it(tmp_path: Path) -> None:
    raw = yaml.safe_load(_fixture_contract(tmp_path).read_text(encoding="utf-8"))
    del raw["config"]["seam_read"]
    path = tmp_path / "no-config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=r"config\.seam_read"):
        _render(path)


def test_a_seam_with_no_reading_and_a_stray_reading_read_unknown(
    tmp_path: Path,
) -> None:
    lines = _render(_fixture_contract(tmp_path), _reading("SM-99", matched=30))
    assert "UNKNOWN seam:SM-00 reason=reading-absent" in lines
    assert "UNKNOWN seam:SM-99 reason=not-in-graph" in lines
    assert not any("status=OK" in line for line in lines)


def test_lines_follow_the_order_of_the_graph(tmp_path: Path) -> None:
    contract = _fixture_contract(tmp_path)
    lines = _render(
        contract, _reading("SM-13", matched=30), _reading("SM-00", matched=30)
    )
    ids = [line.split()[1] for line in lines if line.startswith("SEAM ")]
    assert ids == ["SM-00", "SM-13"]


def test_examples_are_capped_at_three() -> None:
    reading = _reading("SM-02", matched=0, mismatched=5)
    assert reading.mismatched == 5
    assert len(reading.examples) == 3
    with pytest.raises(ValidationError):
        ModelSeamReading(
            seam_id="SM-02",
            window=WINDOW,
            crossings=5,
            mismatched=5,
            unreadable=0,
            examples=("a", "b", "c", "d"),
            read_at=NOW,
        )


def test_the_entry_point_wrapper_renders() -> None:
    assert issubclass(NodeSeamReadingCompute, HandlerSeamReading)


def test_the_verdict_word_lives_in_exactly_one_source_file() -> None:
    hits = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".yaml", ".yml", ".md", ".json"}
        and "MISSING seam" in path.read_text(encoding="utf-8")
    )
    assert hits == ["nodes/node_seam_reading_compute/handlers/handler_seam_reading.py"]


def test_the_reading_and_classify_contracts_declare_no_protocol() -> None:
    for node in ("node_seam_reading_compute", "node_seam_classify_compute"):
        raw = yaml.safe_load(
            (SRC / "nodes" / node / "contract.yaml").read_text(encoding="utf-8")
        )
        assert "protocol_dependencies" not in raw


def test_results_are_byte_identical_in_process_and_after_a_request_round_trip(
    tmp_path: Path,
) -> None:
    contract = _fixture_contract(tmp_path)
    request = ModelSeamReadingRequest(
        readings=(_reading("SM-02", matched=30, mismatched=2),), now=NOW
    )
    direct = HandlerSeamReading(contract).handle(request)
    via_node = HandlerSeamReading(contract).handle(
        ModelSeamReadingRequest.model_validate_json(request.model_dump_json())
    )
    assert direct.model_dump_json() == via_node.model_dump_json()


def test_the_contract_publishes_the_rendered_event_on_its_terminal_topic() -> None:
    raw = yaml.safe_load(
        (SRC / "nodes/node_seam_reading_compute/contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert raw["terminal_event"] == "onex.evt.omnimarket.seam-reading-rendered.v1"
    assert raw["event_bus"]["publish_topics"] == [raw["terminal_event"]]
