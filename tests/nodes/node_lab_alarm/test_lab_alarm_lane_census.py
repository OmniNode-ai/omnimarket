# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane-census conditions of node_lab_alarm and its seam to the alarm delivery.

Golden: a clean snapshot reads OK for both conditions; a snapshot with findings alarms the matching
condition and names the alert key; delivery and run records are written. Error: an unreadable source,
an invalid snapshot, a wrong event type, a mismatched alert key, or a stale/future/malformed
emitted_at is INDETERMINATE; a failed delivery or unwritable record names the failure. Failing
control: the same stale snapshot reads OK with a larger bound; the same finding alarms one condition
and not the other; a drill alarms without reading, delivering, or recording. Seam: the alarm line
builds a valid ModelLabAlarm accepted by the real HandlerLabAlarmDeliver; every emitted topic is
declared in the contract.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import ModelLabAlarm
from omnimarket.nodes.node_lab_alarm.adapters.condition_adapters import (
    CommandCensusSource,
    FileCensusSource,
    InMemoryCensusSource,
)
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryRunRecords,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lane_census import (
    HandlerLaneCensus,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.handlers.lane_census_snapshot import (
    FINDING_KIND_SEVERITY,
    alert_key,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lane_census import (
    EnumLaneCensusCondition,
    EnumLaneCensusFault,
    EnumLaneCensusOutcome,
    ModelLaneCensusCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelLabAlarmOverlay,
    ModelLaneCensusSettings,
)
from omnimarket.nodes.node_lab_alarm.protocols import CensusUnreadableError
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm_conditions import (
    run_lane_census,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CLOCK = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)


def _snapshot(
    findings: list[dict[str, Any]], emitted_at: str | None = None
) -> dict[str, object]:
    if emitted_at is None:
        emitted_at = _CLOCK.isoformat()
    snapshot: dict[str, object] = {
        "schema_version": "1.1.0",
        "event_type": "lane-census-drift",
        "topic": "onex.evt.infra.lane-census-drift.v1",
        "host": "host-1",
        "host_id": "id-1",
        "emitted_at": emitted_at,
        "severity": "info",
        "lanes_checked": ["lane-a"],
        "lanes_not_applicable": [],
        "lanes_skipped_optional_down": [],
        "drift_count": len(findings),
        "findings": findings,
        "ticket_title": "Lane Census",
        "ticket_body": "Body",
    }
    snapshot["alert_key"] = alert_key("host-1", snapshot)
    return snapshot


def _settings(
    max_age_seconds: int = 7200,
    snapshot_path: str = "/tmp/snap.json",
) -> ModelLaneCensusSettings:
    return ModelLaneCensusSettings(
        snapshot_path=snapshot_path,
        snapshot_command=None,
        max_age_seconds=max_age_seconds,
        timeout_seconds=10,
    )


def _command(
    *,
    write: bool = True,
    fault: EnumLaneCensusFault = EnumLaneCensusFault.NONE,
) -> ModelLaneCensusCommand:
    return ModelLaneCensusCommand(correlation_id="census-1", write=write, fault=fault)


def _clean_snapshot() -> dict[str, object]:
    return _snapshot([])


def _findings_snapshot() -> dict[str, object]:
    findings = [
        {
            "lane": "lane-a",
            "kind": "container_unhealthy",
            "container": "c1",
            "detail": "unhealthy",
            "severity": FINDING_KIND_SEVERITY["container_unhealthy"],
        },
        {
            "lane": "lane-b",
            "kind": "container_absent",
            "container": "c2",
            "detail": "absent",
            "severity": FINDING_KIND_SEVERITY["container_absent"],
        },
    ]
    return _snapshot(findings)


class _Rig:
    def __init__(
        self,
        document: dict[str, object] | None = None,
        *,
        condition: EnumLaneCensusCondition = EnumLaneCensusCondition.LANE_DRIFT,
        settings: ModelLaneCensusSettings | None = None,
        deliver: bool = True,
    ) -> None:
        self.source = InMemoryCensusSource(
            document if document is not None else _clean_snapshot()
        )
        self.records = InMemoryRunRecords()
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.handler = HandlerLaneCensus(
            condition=condition,
            settings=settings if settings is not None else _settings(),
            source=self.source,
            delivery=delivery if deliver else None,
            records=self.records,
            clock=lambda: _CLOCK,
        )


def test_census_golden_chain() -> None:
    """A clean snapshot reads OK for both conditions; a snapshot with findings alarms the matching
    condition and names the alert key; delivery and run records are written."""
    topics = load_topics()
    rig = _Rig(condition=EnumLaneCensusCondition.LANE_DRIFT)
    ok = rig.handler.handle(_command())
    assert ok.outcome is EnumLaneCensusOutcome.OK
    assert ok.alarms == ()
    assert ok.reasons == ()
    assert ok.failure == ""
    assert "0 lane_drift findings" in ok.evidence
    assert [t for t, _ in events_for(ok, topics)] == [
        topics["lane_drift_run_started"],
        topics["lane_drift_run_completed"],
    ]
    assert len(rig.records.records) == 1
    assert rig.records.records[0]["reports"][0]["condition"] == "lane_drift"

    rig2 = _Rig(
        document=_findings_snapshot(),
        condition=EnumLaneCensusCondition.CONTAINER_UNHEALTHY,
    )
    alarm = rig2.handler.handle(_command())
    assert alarm.outcome is EnumLaneCensusOutcome.ALARM
    [a] = alarm.alarms
    assert a.startswith(f"{alert_key('host-1', _findings_snapshot())}: ")
    assert "lane-a/c1: container_unhealthy" in a
    assert "lane-b/c2" not in a
    assert [t for t, _ in events_for(alarm, topics)][-1] == topics[
        "container_unhealthy_run_failed"
    ]
    assert alarm.delivery == (
        f"container_unhealthy:{alert_key('host-1', _findings_snapshot())} "
        "channel=delivered ledger=delivered",
    )
    assert len(rig2.records.records) == 1

    rig3 = _Rig(
        document=_findings_snapshot(),
        condition=EnumLaneCensusCondition.LANE_DRIFT,
    )
    alarm2 = rig3.handler.handle(_command())
    assert alarm2.outcome is EnumLaneCensusOutcome.ALARM
    [a2] = alarm2.alarms
    assert "lane-b/c2: container_absent" in a2
    assert "lane-a/c1" not in a2


def test_census_error_chain() -> None:
    """Unreadable source, invalid snapshot, wrong event type, mismatched alert key, stale/future/
    malformed emitted_at are INDETERMINATE; a failed delivery or unwritable record names the failure."""
    topics = load_topics()
    rig = _Rig()
    rig.source.error = "connection refused"
    unreadable = rig.handler.handle(_command())
    assert unreadable.outcome is EnumLaneCensusOutcome.INDETERMINATE
    assert unreadable.reasons == ("census-unreadable",)
    assert unreadable.failure == "indeterminate:census-unreadable"
    assert [t for t, _ in events_for(unreadable, topics)][-1] == topics[
        "lane_drift_run_failed"
    ]

    bad = _clean_snapshot()
    del bad["host"]
    rig2 = _Rig(document=bad)
    invalid = rig2.handler.handle(_command())
    assert invalid.outcome is EnumLaneCensusOutcome.INDETERMINATE
    assert invalid.reasons == ("census-invalid",)

    wrong_type = _clean_snapshot()
    wrong_type["event_type"] = "other"
    rig3 = _Rig(document=wrong_type)
    not_census = rig3.handler.handle(_command())
    assert not_census.outcome is EnumLaneCensusOutcome.INDETERMINATE
    assert not_census.reasons == ("census-not-a-census-event",)

    mismatch = _clean_snapshot()
    mismatch["alert_key"] = "wrong-key"
    rig4 = _Rig(document=mismatch)
    key_mismatch = rig4.handler.handle(_command())
    assert key_mismatch.outcome is EnumLaneCensusOutcome.INDETERMINATE
    assert key_mismatch.reasons == ("census-alert-key-mismatch",)

    stale = _snapshot([], emitted_at="2026-11-14T01:00:00+00:00")
    rig5 = _Rig(document=stale)
    stale_result = rig5.handler.handle(_command())
    assert stale_result.outcome is EnumLaneCensusOutcome.INDETERMINATE
    assert stale_result.reasons == ("census-stale",)

    future = _snapshot([], emitted_at="2026-11-14T05:00:00+00:00")
    rig6 = _Rig(document=future)
    future_result = rig6.handler.handle(_command())
    assert future_result.outcome is EnumLaneCensusOutcome.INDETERMINATE
    assert future_result.reasons == ("census-stale",)

    malformed = _snapshot([], emitted_at="not-a-date")
    rig7 = _Rig(document=malformed)
    malformed_result = rig7.handler.handle(_command())
    assert malformed_result.outcome is EnumLaneCensusOutcome.INDETERMINATE
    assert malformed_result.reasons == ("census-invalid",)

    rig8 = _Rig(document=_findings_snapshot())
    rig8.ledger.refuse = "ledger host unreachable"
    failed = rig8.handler.handle(_command())
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure

    rig9 = _Rig(document=_clean_snapshot())
    rig9.records.unwritable = "disk full"
    unwritable = rig9.handler.handle(_command())
    assert "record-unwritable" in unwritable.failure
    assert "disk full" in unwritable.failure


def test_census_failing_control() -> None:
    """The same stale snapshot reads OK with a larger bound; the same finding alarms one condition
    and not the other; a drill alarms without reading, delivering, or recording."""
    stale = _snapshot([], emitted_at="2026-11-14T01:00:00+00:00")
    rig = _Rig(document=stale, settings=_settings(max_age_seconds=7200))
    assert rig.handler.handle(_command()).outcome is EnumLaneCensusOutcome.INDETERMINATE
    rig2 = _Rig(document=stale, settings=_settings(max_age_seconds=86400))
    assert rig2.handler.handle(_command()).outcome is EnumLaneCensusOutcome.OK

    findings = _findings_snapshot()
    rig3 = _Rig(
        document=findings, condition=EnumLaneCensusCondition.CONTAINER_UNHEALTHY
    )
    assert rig3.handler.handle(_command()).outcome is EnumLaneCensusOutcome.ALARM
    rig4 = _Rig(document=findings, condition=EnumLaneCensusCondition.LANE_DRIFT)
    assert rig4.handler.handle(_command()).outcome is EnumLaneCensusOutcome.ALARM

    rig5 = _Rig(document=_clean_snapshot())
    drill = rig5.handler.handle(_command(fault=EnumLaneCensusFault.PLANTED_FINDING))
    assert drill.outcome is EnumLaneCensusOutcome.ALARM
    assert "planted" in drill.alarms[0]
    assert rig5.records.records == []
    assert rig5.channel.posts == []


def test_census_seam() -> None:
    """The alarm line a run produces, split at the first ': ', builds a valid ModelLabAlarm accepted
    by the real HandlerLabAlarmDeliver; every topic name the handler's events_for emits is declared
    in the contract's topics and externally_consumed_topics."""
    rig = _Rig(document=_findings_snapshot())
    result = rig.handler.handle(_command())
    [alarm] = result.alarms
    subject, separator, detail = alarm.partition(": ")
    assert separator == ": "
    assert subject == alert_key("host-1", _findings_snapshot())
    model = ModelLabAlarm(
        condition=result.condition.value, subject=subject, detail=detail
    )
    assert model.condition == result.condition.value
    assert model.subject == subject
    assert model.detail == detail
    topics = load_topics()
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    declared = set(contract["topics"])
    external = set(contract.get("externally_consumed_topics", []))
    emitted = {t for t, _ in events_for(result, topics)}
    assert {k for k, v in topics.items() if v in emitted} <= declared
    assert emitted <= set(contract["topics"].values())
    assert emitted <= external, "every emitted topic is declared as externally consumed"


def test_census_probe_delivers_and_records_nothing() -> None:
    rig = _Rig(document=_findings_snapshot())
    result = rig.handler.handle(_command(write=False))
    assert result.outcome is EnumLaneCensusOutcome.ALARM
    assert rig.records.records == []
    assert rig.channel.posts == []
    assert result.delivery == ()


def test_census_cli_exit_codes_follow_the_outcome() -> None:
    overlay = ModelLabAlarmOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )
    rig = _Rig(document=_findings_snapshot())
    lines: list[str] = []
    assert (
        run_lane_census(
            "lane-drift-probe",
            EnumLaneCensusFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 2
    )
    assert any(
        line.startswith("LANE-CENSUS condition=lane_drift outcome=ALARM")
        for line in lines
    )
    rig.source.document = _clean_snapshot()
    assert (
        run_lane_census(
            "lane-drift",
            EnumLaneCensusFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 0
    )
    rig.source.error = "connection refused"
    assert (
        run_lane_census(
            "lane-drift",
            EnumLaneCensusFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 3
    )


def test_census_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del raw["lane_census"]
    code = run_lane_census(
        "lane-drift",
        EnumLaneCensusFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
    )
    assert code == 1
    assert "no lane_census block" in capsys.readouterr().err


def test_census_overlay_loads_without_the_block() -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert ModelLabAlarmOverlay.model_validate(raw).lane_census is not None
    del raw["lane_census"]
    assert ModelLabAlarmOverlay.model_validate(raw).lane_census is None


def test_census_file_source_reads_a_file_and_refuses_a_missing_one_and_non_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(_clean_snapshot()), encoding="utf-8")
    doc = FileCensusSource(path).read()
    assert doc["event_type"] == "lane-census-drift"
    with pytest.raises(CensusUnreadableError):
        FileCensusSource(tmp_path / "missing.json").read()
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(CensusUnreadableError):
        FileCensusSource(bad).read()


def test_census_command_source_runs_the_command_and_refuses_failures() -> None:
    def _run_ok(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_clean_snapshot())
        )

    doc = CommandCensusSource(["census"], 10, run=_run_ok).read()
    assert doc["event_type"] == "lane-census-drift"

    def _run_fail(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="")

    with pytest.raises(CensusUnreadableError):
        CommandCensusSource(["census"], 10, run=_run_fail).read()

    def _run_timeout(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(CensusUnreadableError):
        CommandCensusSource(["census"], 10, run=_run_timeout).read()

    def _run_non_json(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="not json")

    with pytest.raises(CensusUnreadableError):
        CommandCensusSource(["census"], 10, run=_run_non_json).read()

    def _run_non_object(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="[1, 2]")

    with pytest.raises(CensusUnreadableError):
        CommandCensusSource(["census"], 10, run=_run_non_object).read()
