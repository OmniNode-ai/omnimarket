# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor cycle publishes the customer health line in the floor record (OMN-22923, piece 17b).

Golden: the line is computed in the cycle from the record's own piece rows, carries the ages those rows carry, is stamped
with the record's time and emits a start and a success. Error: a reading older than its own maximum age makes the written
line NOT WORKING and names the piece; an overlay that cannot be read leaves the line UNKNOWN and the floor still
publishes. The named break (the age test turned off) is run through the same cycle and turns the reading WORKING, so the
stale reading test can fail.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import ModelFloorHealthLine, ModelFloorSimple
from omnimarket.models.floor_17a.model_customer_health import (
    ModelCustomerHealthOverlay,
    ModelCustomerHealthRequest,
    ModelHealthReadingIn,
)
from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_customer_health import (
    HandlerCustomerHealth,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    build_home,
    environ,
    iso,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

RESULT = ".onex_state/readings/central-test-harness.drill.json"
PIECE = 8
STEP = "node_product_floor_verdict_compute:decide_customer_health"


def harness(home: Path, state: str, age_s: int) -> None:
    moment = NOW - timedelta(seconds=age_s)
    path = home / RESULT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "piece": "central-test-harness",
                "state": state,
                "probed_at": iso(moment),
                "published_at": iso(moment),
                "max_age_seconds": 7200,
            }
        ),
        encoding="utf-8",
    )


def own_overlay(home: Path) -> dict[str, str]:
    """The cycle's environment with a health overlay file that names only the piece the harness reads."""
    overlay = home.parent / "health-overlay.yaml"
    overlay.write_text(
        "title: Customer path\nrequired_pieces: [8]\nmax_age_s: 900\n", encoding="utf-8"
    )
    return {
        **environ(home),
        "ONEX_PRODUCT_FLOOR_VERDICT_OVERLAY": str(overlay),
    }


def health_of(doc: ModelFloorSimple) -> ModelFloorHealthLine:
    assert doc.health is not None
    return doc.health


def test_golden_the_record_carries_a_health_line_with_the_ages_of_its_own_rows(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    harness(home, "WORKING", 120)
    sink = InMemoryEmissionSink()
    result = run(home, sink, env=own_overlay(home))
    assert result.ok
    doc = written(home)
    line = health_of(doc)
    row = next(p for p in doc.pieces if p.piece == PIECE)
    assert line.state == "WORKING"
    assert line.generated_at == doc.generated_at
    assert [(r.piece, r.age_s, r.max_age_s, r.verdict) for r in line.readings] == [
        (PIECE, row.reading_age_s, row.reading_max_age_s, "fresh")
    ]
    chain = [e for e in sink.events if e.node == STEP]
    assert [e.phase for e in chain] == ["start", "success"]
    assert chain[0].correlation_id == result.correlation_id


def test_golden_a_checker_can_repeat_the_line_from_the_records_own_piece_rows(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    harness(home, "WORKING", 120)
    env = own_overlay(home)
    assert run(home, env=env).ok
    doc = written(home)
    overlay = ModelCustomerHealthOverlay.model_validate(
        yaml.safe_load(Path(env["ONEX_PRODUCT_FLOOR_VERDICT_OVERLAY"]).read_text())
    )
    again = HandlerCustomerHealth().handle(
        ModelCustomerHealthRequest(
            now=doc.generated_at,
            readings=tuple(
                ModelHealthReadingIn(
                    piece=p.piece,
                    name=p.name,
                    reading_state=p.reading_state,
                    reading_age_s=p.reading_age_s,
                    reading_max_age_s=p.reading_max_age_s,
                )
                for p in doc.pieces
            ),
            overlay=overlay,
        )
    )
    assert again == doc.health


def test_stale_reading_makes_the_written_line_not_working_and_names_the_piece(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    harness(home, "WORKING", 9000)
    assert run(home, env=own_overlay(home)).ok
    line = health_of(written(home))
    assert line.state == "NOT WORKING"
    assert line.named
    assert line.named[0].startswith(f"piece {PIECE} ")
    assert "stale" in line.named[0] or "NOT WORKING" in line.named[0]


def test_error_the_default_overlay_names_four_pieces_and_never_draws_working_without_readings(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    assert run(home).ok
    line = health_of(written(home))
    assert [r.piece for r in line.readings] == [3, 11, 15, 18]
    assert line.state != "WORKING"
    assert len(line.named) >= 1


def test_error_an_overlay_that_cannot_be_read_leaves_the_line_unknown_and_the_floor_still_publishes(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    broken = home.parent / "broken.yaml"
    broken.write_text(
        "title: ''\nrequired_pieces: []\nmax_age_s: 0\n", encoding="utf-8"
    )
    env = {**environ(home), "ONEX_PRODUCT_FLOOR_VERDICT_OVERLAY": str(broken)}
    assert run(home, env=env).ok
    line = health_of(written(home))
    assert line.state == "UNKNOWN"
    assert line.readings == ()
    assert "overlay" in line.named[0]


def test_injected_break_age_test_off_changes_the_verdict_on_the_stale_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    harness(home, "WORKING", 9000)
    assert run(home, env=own_overlay(home)).ok
    assert health_of(written(home)).readings[0].verdict == "stale"
    monkeypatch.setattr(
        HandlerCustomerHealth, "_too_old", staticmethod(lambda _a, _m: False)
    )
    assert run(home, env=own_overlay(home)).ok
    assert health_of(written(home)).readings[0].verdict != "stale"


def test_error_chain_a_failing_health_step_emits_a_typed_failure_and_writes_no_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    harness(home, "WORKING", 120)

    def refuse(_self: HandlerCustomerHealth, _request: object) -> None:
        raise RuntimeError("health refused")

    monkeypatch.setattr(HandlerCustomerHealth, "handle", refuse)
    sink = InMemoryEmissionSink()
    result = run(home, sink, env=own_overlay(home))
    assert not result.ok
    failures = [e for e in sink.events if e.node == STEP and e.phase == "failure"]
    assert len(failures) == 1
    assert failures[0].failure_code
    assert not (home / ".claude_scratch/factory-floor/floor-simple.json").exists()
