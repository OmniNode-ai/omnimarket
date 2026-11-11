# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reading tests of the pilot measurement node (OMN-23619).

The golden case: two pilots, frozen and measured, bar met -> WORKING, row appended to the
ledger with lane, src, drill=no, verdict=WORKING. Then each refusal and rejection path, the
verdict ordering, a failed append, a writing run without an appender, and a probe that appends
nothing. The failing control shows the assertion can fail.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_request import (
    ModelPilotManifestCheckRequest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_result import (
    ModelPilotManifestCheckResult,
)
from omnimarket.nodes.node_pilot_measurement_effect.adapters.inmemory_result_source import (
    InMemoryResultSource,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers import (
    handler_pilot_measurement,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.handler_pilot_measurement import (
    HandlerPilotMeasurement,
)
from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    ModelPilotMeasurementCommand,
    ModelPilotResultRead,
)
from omnimarket.nodes.node_pilot_measurement_effect.protocols import PilotResultError
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_pilot_measurement_effect.pilot_measurement_support import (
    HOST,
    RUN_AT,
    example_overlay_document,
)

pytestmark = pytest.mark.unit

CORRELATION = "reading-1"


def _command(write: bool = True) -> ModelPilotMeasurementCommand:
    return ModelPilotMeasurementCommand(correlation_id=CORRELATION, write=write)


def _doc() -> dict:
    return copy.deepcopy(example_overlay_document())


def _pilot(doc: dict, name: str) -> dict:
    for pilot in doc["pilots"]:
        if pilot["pilot"] == name:
            return pilot
    raise AssertionError(f"pilot {name} not in the example overlay")


def _handler(
    doc: dict,
    *,
    results: dict[str, object] | None = None,
    appender: InMemoryLedger | None = None,
    overlay: object | None = None,
) -> HandlerPilotMeasurement:
    from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
        ModelPilotMeasurementOverlay,
    )

    ov = (
        overlay
        if overlay is not None
        else ModelPilotMeasurementOverlay.model_validate(doc)
    )
    return HandlerPilotMeasurement(
        overlay=ov,
        host=HOST,
        results=InMemoryResultSource(results if results is not None else {}),
        appender=appender,
        clock=lambda: RUN_AT,
    )


def _result(metrics: dict[str, float], measured_at: datetime) -> ModelPilotResultRead:
    return ModelPilotResultRead(metrics=metrics, measured_at=measured_at)


def _good_results() -> dict[str, object]:
    return {
        "example-pilot-refactor": _result(
            {"example_success_rate": 0.95, "example_tokens_per_ticket": 30000.0}, RUN_AT
        ),
        "example-pilot-handoff": _result({"example_handoffs_per_ticket": 1.0}, RUN_AT),
    }


def test_golden_two_pilots_frozen_and_measured_reads_working_and_appends_one_row(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    handler = _handler(_doc(), results=_good_results(), appender=ledger)
    result = handler.handle(_command(write=True))
    assert result.status == "completed"
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.ledger == "written"
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    assert "lane=pilot-measurement" in row
    assert "src=pilot-measurement" in row
    assert "drill=no" in row
    assert "verdict=WORKING" in row
    assert "pilots=2" in row
    assert "accepted=2" in row
    assert "rejected=0" in row
    assert "refused=0" in row
    assert "unmeasured=0" in row


def test_pilot_with_no_bar_hash_reads_not_working(tmp_path: Path) -> None:
    doc = _doc()
    _pilot(doc, "example-pilot-refactor")["bar_hash"] = None
    handler = _handler(doc, results=_good_results(), appender=InMemoryLedger())
    result = handler.handle(_command(write=True))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any(
        r.startswith("pilot-refused:example-pilot-refactor:") for r in result.reasons
    )


def test_pilot_with_wrong_bar_hash_reads_not_working(tmp_path: Path) -> None:
    doc = _doc()
    _pilot(doc, "example-pilot-refactor")["bar_hash"] = (
        "00000000000000000000000000000000"
    )
    handler = _handler(doc, results=_good_results(), appender=InMemoryLedger())
    result = handler.handle(_command(write=True))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any(
        r.startswith("pilot-refused:example-pilot-refactor:") for r in result.reasons
    )


def test_pilot_started_before_frozen_reads_not_working(tmp_path: Path) -> None:
    doc = _doc()
    pilot = _pilot(doc, "example-pilot-refactor")
    frozen = pilot["frozen_at"]
    pilot["started_at"] = frozen
    pilot["frozen_at"] = (RUN_AT - timedelta(hours=1)).isoformat()
    handler = _handler(doc, results=_good_results(), appender=InMemoryLedger())
    result = handler.handle(_command(write=True))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any(
        r.startswith("pilot-refused:example-pilot-refactor:") for r in result.reasons
    )


def test_result_file_missing_reads_unknown(tmp_path: Path) -> None:
    handler = _handler(
        _doc(),
        results={
            "example-pilot-refactor": PilotResultError("result-unreadable"),
            "example-pilot-handoff": _result(
                {"example_handoffs_per_ticket": 1.0}, RUN_AT
            ),
        },
        appender=InMemoryLedger(),
    )
    result = handler.handle(_command(write=True))
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert any(
        r.startswith("pilot-unmeasured:example-pilot-refactor:") for r in result.reasons
    )


def test_result_stale_reads_unknown(tmp_path: Path) -> None:
    stale = RUN_AT - timedelta(seconds=100000)
    handler = _handler(
        _doc(),
        results={
            "example-pilot-refactor": _result(
                {"example_success_rate": 0.95, "example_tokens_per_ticket": 30000.0},
                stale,
            ),
            "example-pilot-handoff": _result(
                {"example_handoffs_per_ticket": 1.0}, RUN_AT
            ),
        },
        appender=InMemoryLedger(),
    )
    result = handler.handle(_command(write=True))
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert any(
        r.startswith("pilot-unmeasured:example-pilot-refactor:") for r in result.reasons
    )


def test_metric_below_threshold_is_rejected_and_reads_the_overlay_rejected_reading(
    tmp_path: Path,
) -> None:
    doc = _doc()
    doc["publish"]["rejected_reading"] = "NOT_WORKING"
    handler = _handler(
        doc,
        results={
            "example-pilot-refactor": _result(
                {"example_success_rate": 0.1, "example_tokens_per_ticket": 30000.0},
                RUN_AT,
            ),
            "example-pilot-handoff": _result(
                {"example_handoffs_per_ticket": 1.0}, RUN_AT
            ),
        },
        appender=InMemoryLedger(),
    )
    result = handler.handle(_command(write=True))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any(
        r.startswith("pilot-rejected:example-pilot-refactor:") for r in result.reasons
    )


def test_rejected_reading_setting_working_reads_working(tmp_path: Path) -> None:
    doc = _doc()
    doc["publish"]["rejected_reading"] = "WORKING"
    handler = _handler(
        doc,
        results={
            "example-pilot-refactor": _result(
                {"example_success_rate": 0.1, "example_tokens_per_ticket": 30000.0},
                RUN_AT,
            ),
            "example-pilot-handoff": _result(
                {"example_handoffs_per_ticket": 1.0}, RUN_AT
            ),
        },
        appender=InMemoryLedger(),
    )
    result = handler.handle(_command(write=True))
    assert result.verdict is EnumReadingVerdict.WORKING
    assert any(
        r.startswith("pilot-rejected:example-pilot-refactor:") for r in result.reasons
    )


def test_refused_beats_unmeasured(tmp_path: Path) -> None:
    doc = _doc()
    _pilot(doc, "example-pilot-refactor")["bar_hash"] = None
    handler = _handler(
        doc,
        results={
            "example-pilot-refactor": PilotResultError("result-unreadable"),
            "example-pilot-handoff": _result(
                {"example_handoffs_per_ticket": 1.0}, RUN_AT
            ),
        },
        appender=InMemoryLedger(),
    )
    result = handler.handle(_command(write=True))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any(
        r.startswith("pilot-refused:example-pilot-refactor:") for r in result.reasons
    )


def test_failed_append_is_a_failure_and_the_chain_ends_in_reading_not_written(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    handler = _handler(_doc(), results=_good_results(), appender=ledger)
    result = handler.handle(_command(write=True))
    assert result.status == "completed"
    assert result.ledger == "append-failed:refused"
    assert ledger.lines == []


def test_writing_run_without_appender_fails(tmp_path: Path) -> None:
    handler = _handler(_doc(), results=_good_results(), appender=None)
    result = handler.handle(_command(write=True))
    assert result.status == "failed"
    assert result.failure == "no-appender-for-a-writing-run"


def test_probe_appends_nothing(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    handler = _handler(_doc(), results=_good_results(), appender=ledger)
    result = handler.handle(_command(write=False))
    assert result.status == "completed"
    assert result.ledger == "not-written"
    assert ledger.lines == []


def test_a_pilot_with_no_frozen_bar_never_reads_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: with the manifest check replaced by one that always accepts, the
    verdict WOULD be WORKING; the real check reads NOT_WORKING, so the assertion can fail."""
    doc = _doc()
    _pilot(doc, "example-pilot-refactor")["bar_hash"] = None

    class _AlwaysAccept:
        """A manifest check that accepts every pilot: the break the control injects."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def handle(self, request: ModelPilotManifestCheckRequest) -> object:
            return ModelPilotManifestCheckResult(
                correlation_id=request.correlation_id,
                pilot=request.manifest.pilot,
                check_passed=True,
                status=EnumPilotStatus.ACCEPTED,
            )

    monkeypatch.setattr(
        handler_pilot_measurement, "HandlerPilotManifestCheck", _AlwaysAccept
    )
    broken = _handler(doc, results=_good_results(), appender=InMemoryLedger())
    assert broken.handle(_command(write=True)).verdict is EnumReadingVerdict.WORKING

    monkeypatch.undo()
    real = _handler(doc, results=_good_results(), appender=InMemoryLedger())
    assert real.handle(_command(write=True)).verdict is EnumReadingVerdict.NOT_WORKING
