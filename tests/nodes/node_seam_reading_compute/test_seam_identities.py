# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam reading counts identities, not elements (OMN-21508)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.seam_reading import (
    EnumSeamCrossingState,
    ModelSeamCrossing,
    ModelSeamReading,
    ModelSeamWindow,
)
from omnimarket.nodes.node_seam_reading_compute.handlers.handler_seam_reading import (
    ORCHESTRATOR_CONTRACT_PATH,
    HandlerSeamReading,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_seam_reading_request import (
    ModelSeamReadingRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
WINDOW = ModelSeamWindow(start=NOW - timedelta(hours=24), end=NOW)
MODELS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "models"


def _healthy(*keys: str) -> list[ModelSeamCrossing]:
    return [ModelSeamCrossing(key=k, state=EnumSeamCrossingState.MATCHED) for k in keys]


def _defective(key: str) -> ModelSeamCrossing:
    return ModelSeamCrossing(
        key=key, state=EnumSeamCrossingState.MISMATCHED, reason="engine differs"
    )


def _contract(tmp_path: Path, minimum: int = 5) -> Path:
    raw = yaml.safe_load(ORCHESTRATOR_CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["config"]["seam_read"]["default_minimum_crossings"] = minimum
    for node in raw["workflow_coordination"]["workflow_definition"]["execution_graph"][
        "nodes"
    ]:
        step = node.get("step_config")
        if step is not None:
            step["expected_crossings"] = 100
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _lines(tmp_path: Path, reading: ModelSeamReading) -> list[str]:
    result = HandlerSeamReading(_contract(tmp_path)).handle(
        ModelSeamReadingRequest(readings=(reading,), now=NOW)
    )
    return [line for line in result.lines if "SM-02" in line]


def _read(
    crossings: list[ModelSeamCrossing],
    expected: list[str] | None,
    cursor: str | None = "cursor-1",
) -> ModelSeamReading:
    return ModelSeamReading.from_crossings(
        "SM-02",
        WINDOW,
        crossings,
        NOW,
        expected_identities=expected,
        source_cursor=cursor,
    )


def test_two_crossings_of_one_key_are_refused_naming_the_key() -> None:
    crossings = _healthy("a", "b", "dup", "dup")
    with pytest.raises(ValueError, match="dup"):
        _read(crossings, ["a", "b", "dup"])


def test_a_defective_identity_replaced_by_a_distinct_healthy_one_does_not_read_ok(
    tmp_path: Path,
) -> None:
    expected = ["a", "b", "c", "d", "e", "defective"]
    crossings = _healthy("a", "b", "c", "d", "e", "stand-in")
    assert len(crossings) == len(expected)

    reading = _read(crossings, expected)

    assert reading.mismatched == 1
    assert reading.unexpected == 1
    assert reading.examples == ("defective",)
    lines = _lines(tmp_path, reading)
    assert lines[0].endswith("status=MISSING")
    assert not any("status=OK" in line for line in lines)


def test_an_unexpected_crossing_cannot_raise_the_crossing_count(tmp_path: Path) -> None:
    expected = ["a", "b", "c", "d", "e"]
    padded = _read(_healthy("a", "b", "c", "d", "e", "extra"), expected)
    plain = _read(_healthy("a", "b", "c", "d", "e"), expected)

    assert padded.crossings == plain.crossings == 5
    assert padded.unexpected == 1
    assert "UNKNOWN seam:SM-02 reason=unexpected=1" in _lines(tmp_path, padded)
    assert _lines(tmp_path, plain) == [
        "SEAM SM-02 crossings=5 mismatched=0 unreadable=0 window=24h status=OK"
    ]


def test_an_expected_identity_without_a_crossing_is_mismatched(tmp_path: Path) -> None:
    reading = _read(_healthy("a", "b", "c", "d", "e"), ["a", "b", "c", "d", "e", "f"])
    assert reading.crossings == 6
    assert reading.mismatched == 1
    assert reading.examples == ("f",)
    assert _lines(tmp_path, reading)[0].endswith("status=MISSING")


@pytest.mark.parametrize(
    ("expected", "cursor"),
    [(None, "cursor-1"), (["a", "b", "c", "d", "e"], None), (None, None), (["a"], "")],
)
def test_missing_identity_evidence_reads_unknown(
    tmp_path: Path, expected: list[str] | None, cursor: str | None
) -> None:
    reading = _read(_healthy("a", "b", "c", "d", "e"), expected, cursor)
    lines = _lines(tmp_path, reading)
    assert "UNKNOWN seam:SM-02 reason=identity-evidence-absent" in lines
    assert not any("status=OK" in line for line in lines)


def test_a_reading_round_trips_byte_identical() -> None:
    reading = _read(_healthy("a", "b", "extra"), ["a", "b", "gone"])
    again = ModelSeamReading.model_validate_json(reading.model_dump_json())
    assert again == reading
    assert again.model_dump_json() == reading.model_dump_json()


def test_the_model_package_depends_on_no_protocol() -> None:
    for path in (MODELS / "seam_reading").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "protocol" not in text.lower(), path.name
