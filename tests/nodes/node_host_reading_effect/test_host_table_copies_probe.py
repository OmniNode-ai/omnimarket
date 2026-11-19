# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-table-copies probe of the hourly host reading (OMN-24558).

The drift read of the lab host table's copies has its home as one more probe of
node_host_reading_effect. A scripted copy source stands in for the files: three copies of a
synthetic table, and a named break per verdict. The control shows the NOT_WORKING assertion can
fail: run against a probe that returns WORKING, it is red.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.models.model_host_reading import (
    EnumHostProbe,
    ModelHostReadingCommand,
    ModelProbeReading,
)
from omnimarket.models.model_host_table_copies_drift import (
    ModelCopyValue,
    ModelFieldDifference,
    ModelHostTableDriftCommand,
    ModelHostTableDriftResult,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.adapters.host_table_copies import (
    DriftNodeCopies,
)
from omnimarket.nodes.node_host_reading_effect.handlers import (
    probe_host_table_copies as probe_module,
)
from omnimarket.nodes.node_host_reading_effect.handlers.handler_host_reading import (
    HandlerHostReading,
)
from omnimarket.nodes.node_host_reading_effect.handlers.probe_host_table_copies import (
    read_host_table_copies,
)
from omnimarket.nodes.node_host_reading_effect.overlay import ModelHostReadingOverlay
from omnimarket.nodes.node_host_reading_effect.protocols import (
    ProtocolHostTableCopies,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.handlers.handler_host_table_copies_drift import (
    HandlerHostTableCopiesDrift,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.overlay import (
    ModelHostTableCopiesDriftOverlay,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.protocols import (
    CopyReadError,
    ProtocolCopySource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from tests.nodes.node_host_reading_effect.host_reading_support import (
    EXAMPLE,
    RUN_AT,
    World,
)
from tests.nodes.node_host_reading_effect.test_host_reading_chain_events import (
    CORRELATION,
    TOPICS,
    _publish,
    assert_failed_chain,
)
from tests.nodes.node_host_table_copies_drift_compute import drift_support
from tests.nodes.node_host_table_copies_drift_compute.drift_support import (
    World as TableWorld,
)

pytestmark = pytest.mark.unit

TABLE_HOST = "example-table-host"
PAYLOAD = json.dumps(
    {"correlation_id": CORRELATION, "probes": ["host-table-copies"], "write": True}
)
# A value no reading may carry: a target that differs in one copy.
SECRET_VALUE = "leaky-target.example.test"


class ScriptedCopies:
    """The drift node's overlay and a scripted source: what a test breaks, it breaks here."""

    def __init__(
        self,
        source: ProtocolCopySource,
        overlay_error: NodeOverlayError | None = None,
    ) -> None:
        self._source = source
        self._overlay_error = overlay_error
        self.sources_built = 0

    def overlay(self) -> ModelHostTableCopiesDriftOverlay:
        if self._overlay_error is not None:
            raise self._overlay_error
        return drift_support.overlay()

    def source(self, overlay: ModelHostTableCopiesDriftOverlay) -> ProtocolCopySource:
        self.sources_built += 1
        return self._source


class RaisingSource:
    """A copy source whose read raises, as a copy that cannot be read does."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def text(self, name: str) -> str:
        raise self._error


def host_overlay() -> ModelHostReadingOverlay:
    return ModelHostReadingOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def handler(
    copies: ProtocolHostTableCopies | None,
    tmp: Path,
    ledger: InMemoryLedger | None = None,
) -> HandlerHostReading:
    return HandlerHostReading(
        overlay=host_overlay(),
        host=TABLE_HOST,
        files=World("memory", tmp).files,
        appender=ledger or InMemoryLedger(),
        clock=lambda: RUN_AT,
        environ={},
        table_copies=copies,
    )


def reading_of(source: ProtocolCopySource) -> ModelProbeReading:
    return read_host_table_copies(ScriptedCopies(source), TABLE_HOST, lambda: RUN_AT)


def third_copy_differs() -> TableWorld:
    """The failing control's world: the third copy holds one field of one host differently."""
    world = TableWorld()
    world.break_change_a_cap(copy="overlays", host="host-a", cap=9)
    for row in world.hosts["overlays"]:
        if row["name"] == "host-a":
            row["target"] = SECRET_VALUE
    return world


# -- golden chain ----------------------------------------------------------------------------


def test_golden_chain_copies_agree_appends_a_working_row_and_a_golden_event(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    agreeing = handler(ScriptedCopies(TableWorld().source()), tmp_path, ledger)
    events = asyncio.run(
        _publish(
            World("memory", tmp_path),
            ledger,
            PAYLOAD.encode(),
            host=TABLE_HOST,
            handler=agreeing,
        )
    )
    assert [topic for topic, _ in events] == [
        TOPICS["host_reading_written"],
        TOPICS["host_reading_run_completed"],
    ]
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    written = events[0][1]
    assert written["probe"] == "host-table-copies"
    assert written["verdict"] == "WORKING"
    assert written["outcome"] == "read"
    assert written["cells"] == {
        "copies": "3",
        "hosts": "2",
        "differing_hosts": "0",
        "differing_fields": "0",
    }
    assert events[-1][1]["failed"] == 0
    (row,) = ledger.lines
    for cell in (
        "src=host-reading",
        "probe=host-table-copies",
        "verdict=WORKING",
        f"host={TABLE_HOST}",
        "injected fault: Change one field of one host in one declared copy",
    ):
        assert cell in row


def test_the_probe_runs_the_drift_read_in_probe_mode_with_no_appender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Spy(HandlerHostTableCopiesDrift):
        def __init__(
            self,
            *,
            overlay: ModelHostTableCopiesDriftOverlay,
            host: str,
            source: ProtocolCopySource,
            appender: ProtocolReadingAppender | None,
            clock: Callable[[], datetime],
        ) -> None:
            seen["appender"] = appender
            super().__init__(
                overlay=overlay,
                host=host,
                source=source,
                appender=appender,
                clock=clock,
            )

        def handle(
            self, request: ModelHostTableDriftCommand
        ) -> ModelHostTableDriftResult:
            seen["write"] = request.write
            return super().handle(request)

    monkeypatch.setattr(probe_module, "HandlerHostTableCopiesDrift", Spy)
    assert reading_of(TableWorld().source()).verdict is EnumReadingVerdict.WORKING
    assert seen == {"appender": None, "write": False}


# -- error chain -----------------------------------------------------------------------------


def test_error_chain_a_copy_that_cannot_be_read_is_unknown_with_a_failure_event(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    unreadable = handler(
        ScriptedCopies(RaisingSource(CopyReadError("config: file-missing"))),
        tmp_path,
        ledger,
    )
    events = asyncio.run(
        _publish(
            World("memory", tmp_path),
            ledger,
            PAYLOAD.encode(),
            host=TABLE_HOST,
            handler=unreadable,
        )
    )
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    (row,) = ledger.lines
    assert "probe=host-table-copies" in row
    assert "verdict=UNKNOWN" in row
    assert "reason=copy-unreadable-" in row
    assert "file-missing" in row


def test_error_chain_a_source_that_raises_an_os_error_is_a_typed_unknown(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    broken = handler(ScriptedCopies(RaisingSource(OSError("disk"))), tmp_path, ledger)
    events = asyncio.run(
        _publish(
            World("memory", tmp_path),
            ledger,
            PAYLOAD.encode(),
            host=TABLE_HOST,
            handler=broken,
        )
    )
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    (row,) = ledger.lines
    assert "reason=host-read-failed-oserror" in row


def test_a_missing_drift_overlay_is_unknown_with_a_typed_reason() -> None:
    copies = ScriptedCopies(
        TableWorld().source(), overlay_error=NodeOverlayNotFoundError("none")
    )
    reading = read_host_table_copies(copies, TABLE_HOST, lambda: RUN_AT)
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert reading.reasons == ("drift-overlay-not-found",)
    assert copies.sources_built == 0


def test_an_invalid_drift_overlay_is_unknown_with_a_typed_reason() -> None:
    copies = ScriptedCopies(
        TableWorld().source(), overlay_error=NodeOverlayError("bad")
    )
    reading = read_host_table_copies(copies, TABLE_HOST, lambda: RUN_AT)
    assert reading.reasons == ("drift-overlay-invalid",)


def test_ports_not_wired_is_unknown_not_a_crash(tmp_path: Path) -> None:
    (outcome,) = (
        handler(None, tmp_path)
        .handle(
            ModelHostReadingCommand(
                correlation_id="c",
                probes=(EnumHostProbe.HOST_TABLE_COPIES,),
                write=False,
            )
        )
        .probes
    )
    assert outcome.verdict is EnumReadingVerdict.UNKNOWN
    assert outcome.reasons == ("host-table-copies-ports-not-wired",)


def test_a_copy_that_is_not_a_table_is_unknown() -> None:
    world = TableWorld()
    world.break_make_a_copy_not_a_table()
    reading = reading_of(world.source())
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert reading.reasons[0].startswith("copy-unreadable-")


# -- the failing control ---------------------------------------------------------------------


def assert_names_the_drifting_host_and_field(
    reading: ModelProbeReading, row: str | None = None
) -> None:
    """The control's assertion: NOT_WORKING, the host and the field named, no value shown."""
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING, reading.verdict
    assert reading.outcome is EnumReadingOutcome.READ
    assert "drift-host-a-cap" in reading.reasons, reading.reasons
    assert "drift-host-a-target" in reading.reasons, reading.reasons
    assert reading.cells["differing_hosts"] == "1"
    assert reading.cells["differing_fields"] == "2"
    shown = " ".join([*reading.reasons, *reading.cells.values(), row or ""])
    assert SECRET_VALUE not in shown


def test_control_a_scripted_third_copy_that_differs_is_not_working_naming_host_and_field() -> (
    None
):
    assert_names_the_drifting_host_and_field(reading_of(third_copy_differs().source()))


def test_control_through_the_chain_the_row_and_event_name_the_drift_and_show_no_value(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    drifting = handler(ScriptedCopies(third_copy_differs().source()), tmp_path, ledger)
    events = asyncio.run(
        _publish(
            World("memory", tmp_path),
            ledger,
            PAYLOAD.encode(),
            host=TABLE_HOST,
            handler=drifting,
        )
    )
    # NOT WORKING is a reading, so the chain still ends in success.
    assert [topic for topic, _ in events] == [
        TOPICS["host_reading_written"],
        TOPICS["host_reading_run_completed"],
    ]
    (row,) = ledger.lines
    assert "verdict=NOT_WORKING" in row
    assert "drift-host-a-cap" in row
    assert SECRET_VALUE not in row
    assert SECRET_VALUE not in str(events)
    assert events[0][1]["verdict"] == "NOT_WORKING"


def test_control_the_assertion_is_red_against_a_probe_that_returns_working() -> None:
    """Bypass the probe: a stub that answers WORKING must fail the control's assertion."""
    bypassed = ModelProbeReading(
        probe=EnumHostProbe.HOST_TABLE_COPIES,
        verdict=EnumReadingVerdict.WORKING,
        outcome=EnumReadingOutcome.READ,
        reasons=("copies-agree-3-copies-2-hosts",),
        cells={
            "copies": "3",
            "hosts": "2",
            "differing_hosts": "0",
            "differing_fields": "0",
        },
    )
    with pytest.raises(AssertionError):
        assert_names_the_drifting_host_and_field(bypassed)


def test_control_the_assertion_is_red_when_a_value_leaks_into_the_row() -> None:
    reading = reading_of(third_copy_differs().source())
    with pytest.raises(AssertionError):
        assert_names_the_drifting_host_and_field(
            reading, row=f"differing={SECRET_VALUE}"
        )


def test_only_the_named_number_of_differences_are_named() -> None:
    world = TableWorld()
    for row in world.hosts["overlays"]:
        row["cap"] = 99
        row["extra_a"] = 1
        row["extra_b"] = 2
    overlay_document = drift_support.overlay_document()
    overlay_document["publish"]["max_named"] = 2
    limited = ModelHostTableCopiesDriftOverlay.model_validate(overlay_document)

    class Limited(ScriptedCopies):
        def overlay(self) -> ModelHostTableCopiesDriftOverlay:
            return limited

    reading = read_host_table_copies(
        Limited(world.source()), TABLE_HOST, lambda: RUN_AT
    )
    named = [reason for reason in reading.reasons if reason.startswith("drift-host-")]
    assert len(named) == 2
    assert "and-4-more" in reading.reasons
    assert reading.cells["differing_fields"] == "6"


# -- the drift node's own loader and source ----------------------------------------------------


def drift_root(tmp_path: Path, world: TableWorld) -> dict[str, str]:
    """A real overlay root and real copy files, as the node reads them on a host."""
    paths: dict[str, str] = {}
    for name, text in world.texts().items():
        path = tmp_path / "copies" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths[name] = str(path)
    overlay_file = (
        tmp_path / "roots" / "node_host_table_copies_drift_compute" / "overlay.yaml"
    )
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text(
        yaml.safe_dump(drift_support.overlay_document(paths)), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "roots")}


def test_the_drift_nodes_loader_and_local_source_read_real_copies(
    tmp_path: Path,
) -> None:
    environ = drift_root(tmp_path, TableWorld())
    reading = read_host_table_copies(
        DriftNodeCopies(environ), TABLE_HOST, lambda: RUN_AT
    )
    assert reading.verdict is EnumReadingVerdict.WORKING
    drifting = TableWorld()
    drifting.break_change_a_cap()
    reading = read_host_table_copies(
        DriftNodeCopies(drift_root(tmp_path / "other", drifting)),
        TABLE_HOST,
        lambda: RUN_AT,
    )
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert "drift-host-a-cap" in reading.reasons


def test_with_no_overlay_root_the_probe_reads_unknown_not_found() -> None:
    reading = read_host_table_copies(
        DriftNodeCopies({"ONEX_SKILL_OVERLAY_ROOTS": "/nonexistent-root"}),
        TABLE_HOST,
        lambda: RUN_AT,
    )
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.reasons == ("drift-overlay-not-found",)


def test_a_missing_copy_file_reads_unknown(tmp_path: Path) -> None:
    environ = drift_root(tmp_path, TableWorld())
    (tmp_path / "copies" / "config.yaml").unlink()
    reading = read_host_table_copies(
        DriftNodeCopies(environ), TABLE_HOST, lambda: RUN_AT
    )
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.outcome is EnumReadingOutcome.SOURCE_UNREADABLE


# -- the host reading overlay -------------------------------------------------------------------


def test_the_overlay_accepts_the_probe_in_a_hosts_probes() -> None:
    host = host_overlay().host(TABLE_HOST)
    assert host is not None
    assert host.probes == (EnumHostProbe.HOST_TABLE_COPIES,)
    assert EnumHostProbe("host-table-copies") is EnumHostProbe.HOST_TABLE_COPIES


def test_the_overlay_refuses_the_probe_with_no_settings() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["host_table_copies"]
    with pytest.raises(ValueError, match="host-table-copies with no host_table_copies"):
        ModelHostReadingOverlay.model_validate(document)


def test_the_overlay_still_refuses_an_unknown_probe_name() -> None:
    document = copy.deepcopy(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    document["hosts"][-1]["probes"] = ["host-table-copies", "host-table-copy"]
    with pytest.raises(ValueError, match="host-table-copy"):
        ModelHostReadingOverlay.model_validate(document)


def test_the_overlay_hash_moves_with_the_settings() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    changed = copy.deepcopy(document)
    changed["host_table_copies"]["injected_fault"] = "another fault"
    assert (
        ModelHostReadingOverlay.model_validate(document).overlay_hash()
        != ModelHostReadingOverlay.model_validate(changed).overlay_hash()
    )


def test_a_command_naming_the_probe_on_a_host_that_does_not_declare_it_fails(
    tmp_path: Path,
) -> None:
    other = HandlerHostReading(
        overlay=host_overlay(),
        host="example-writing-host",
        files=World("memory", tmp_path).files,
        appender=None,
        clock=lambda: RUN_AT,
        environ={},
        table_copies=ScriptedCopies(TableWorld().source()),
    )
    result = other.handle(
        ModelHostReadingCommand(
            correlation_id="c", probes=(EnumHostProbe.HOST_TABLE_COPIES,), write=False
        )
    )
    assert result.status == "failed"
    assert result.failure == "probes-not-declared:host-table-copies"


# -- the seam: the drift node's result model the probe reads -----------------------------------------


def test_seam_the_probe_imports_the_drift_nodes_handler_and_result_model() -> None:
    assert probe_module.HandlerHostTableCopiesDrift is HandlerHostTableCopiesDrift
    assert probe_module.ModelHostTableDriftResult is ModelHostTableDriftResult
    assert probe_module.ModelHostTableDriftCommand is ModelHostTableDriftCommand
    assert HandlerHostTableCopiesDrift.__module__ == (
        "omnimarket.nodes.node_host_table_copies_drift_compute.handlers."
        "handler_host_table_copies_drift"
    )
    parameters = set(inspect.signature(HandlerHostTableCopiesDrift).parameters)
    assert parameters == {"overlay", "host", "source", "appender", "clock"}


def test_seam_the_drift_result_model_keeps_the_verdict_enum_and_field_names_the_probe_reads() -> (
    None
):
    assert {member.name: member.value for member in EnumReadingVerdict} == {
        "WORKING": "WORKING",
        "NOT_WORKING": "NOT_WORKING",
        "UNKNOWN": "UNKNOWN",
    }
    assert set(ModelHostTableDriftResult.model_fields) == {
        "correlation_id",
        "status",
        "host",
        "failure",
        "verdict",
        "outcome",
        "reasons",
        "copies",
        "hosts_compared",
        "differing_hosts",
        "differences",
        "declared",
        "stale_declarations",
        "row_key",
        "ledger",
        "row",
    }
    assert ModelHostTableDriftResult.model_fields["verdict"].annotation is (
        EnumReadingVerdict
    )
    assert set(ModelFieldDifference.model_fields) == {"host", "field", "values"}
    assert set(ModelCopyValue.model_fields) == {"copy_name", "value"}
    assert set(ModelHostTableDriftCommand.model_fields) == {"correlation_id", "write"}


def test_seam_the_drift_overlay_keeps_the_publish_bound_the_probe_reads() -> None:
    assert drift_support.overlay().publish.max_named >= 1
    assert "copies" in ModelHostTableCopiesDriftOverlay.model_fields


def test_seam_a_real_drift_result_has_the_values_the_probe_maps() -> None:
    result = HandlerHostTableCopiesDrift(
        overlay=drift_support.overlay(),
        host=TABLE_HOST,
        source=third_copy_differs().source(),
        appender=None,
        clock=lambda: RUN_AT,
    ).handle(ModelHostTableDriftCommand(correlation_id="c", write=False))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert {(item.host, item.field) for item in result.differences} == {
        ("host-a", "cap"),
        ("host-a", "target"),
    }
    assert result.differing_hosts == 1
