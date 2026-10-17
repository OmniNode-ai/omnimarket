# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: bad readings, a broken table and a dark collector (OMN-17427)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_lab_sli import EnumLabSliStatus
from omnimarket.models.lab_sli import (
    ModelLabSliReading,
    ModelLabSliReadingBatch,
    ModelLabSliSubjectReading,
)
from omnimarket.nodes.node_lab_sli_compute.handlers.handler_lab_sli_evaluate import (
    HandlerLabSliEvaluate,
    load_sli_table,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)
NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


def _batch(
    *readings: ModelLabSliReading, errors: tuple[str, ...] = ()
) -> ModelLabSliReadingBatch:
    return ModelLabSliReadingBatch(
        interval_start=T0,
        collected_at=T0 + timedelta(minutes=1),
        collector_host="h101",
        readings=readings,
        source_errors=errors,
    )


def test_an_unknown_indicator_and_a_repeat_are_rejected_not_evaluated() -> None:
    one = ModelLabSliReading(
        sli_id="push_refusal_rate",
        subjects=(ModelLabSliSubjectReading(subject="lab", value=0.0),),
    )
    batch = HandlerLabSliEvaluate().handle(
        _batch(one, one, ModelLabSliReading(sli_id="no_such_indicator"))
    )
    assert batch.rejected == (
        "duplicate:push_refusal_rate",
        "unknown:no_such_indicator",
    )
    by_id = {e.sli_id: e for e in batch.evaluations}
    assert by_id["push_refusal_rate"].status is EnumLabSliStatus.OK
    assert "no_such_indicator" not in by_id


def test_a_dark_collector_reads_every_indicator_unknown_and_keeps_the_reason() -> None:
    batch = HandlerLabSliEvaluate().handle(
        _batch(errors=("pr-watcher-state: missing",))
    )
    statuses = {e.sli_id: e.status for e in batch.evaluations}
    measured = {k: v for k, v in statuses.items() if k != "sli_source_coverage"}
    assert set(measured.values()) == {EnumLabSliStatus.UNKNOWN}
    assert statuses["sli_source_coverage"] is EnumLabSliStatus.BREACH
    assert batch.source_errors == ("pr-watcher-state: missing",)


def test_an_applicable_reading_with_no_subjects_is_unknown() -> None:
    batch = HandlerLabSliEvaluate().handle(
        _batch(
            ModelLabSliReading(
                sli_id="host_disk_free_gb", detail="probe returned nothing"
            )
        )
    )
    by_id = {e.sli_id: e for e in batch.evaluations}
    assert by_id["host_disk_free_gb"].status is EnumLabSliStatus.UNKNOWN
    assert by_id["host_disk_free_gb"].detail == "probe returned nothing"


def test_a_table_missing_a_field_fails_closed(tmp_path: Path) -> None:
    contract = yaml.safe_load(
        (NODES / "node_lab_sli_compute" / "contract.yaml").read_text()
    )
    del contract["sli_table"][0]["severity"]
    broken = tmp_path / "contract.yaml"
    broken.write_text(yaml.safe_dump(contract))
    with pytest.raises(ValueError, match="severity"):
        load_sli_table(broken)


def test_a_repeated_indicator_id_in_the_table_fails_closed(tmp_path: Path) -> None:
    contract = yaml.safe_load(
        (NODES / "node_lab_sli_compute" / "contract.yaml").read_text()
    )
    contract["sli_table"].append(contract["sli_table"][0])
    broken = tmp_path / "contract.yaml"
    broken.write_text(yaml.safe_dump(contract))
    with pytest.raises(ValueError, match="repeated"):
        load_sli_table(broken)


def test_no_threshold_or_severity_literal_lives_in_python() -> None:
    offenders: list[str] = []
    for node in ("node_lab_sli_compute", "node_lab_sli_alert_effect"):
        for path in (NODES / node).rglob("*.py"):
            text = path.read_text()
            if re.search(r"EnumLabSliSeverity\.SEV[1-4]", text):
                offenders.append(f"{path.name}: severity literal")
            if re.search(r"(target|renotify\w*|threshold)\s*=\s*\d", text):
                offenders.append(f"{path.name}: threshold literal")
    assert offenders == []


@pytest.mark.parametrize(
    ("node", "terminal_event"),
    [
        ("node_lab_sli_compute", "onex.evt.omnimarket.lab-sli-evaluated.v1"),
        ("node_lab_sli_alert_effect", "onex.evt.omnimarket.lab-sli-alerted.v1"),
        ("node_lab_sli_board_compute", "onex.evt.omnimarket.lab-sli-board-rendered.v1"),
    ],
)
def test_each_node_publishes_its_declared_terminal_event(
    node: str, terminal_event: str
) -> None:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert contract["terminal_event"] == terminal_event
    assert terminal_event in contract["event_bus"]["publish_topics"]
