# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J4, overlays to secrets (OMN-22912, plan sections 3.3 and 4.1): golden and error join tests.

A node resolves one key through its overlay from the store; a scan of its code, its log and the
ledger row it writes finds no value. Every test reads the join back through the floor's own
receipt fold (``HandlerFloorReceipt``), so what is asserted is what the floor counts. Each error
test has its control beside it: the same run with the injected break removed must read WORKING,
which shows the error assertions can fail. No assertion prints or compares a value in a message.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelPieceDef,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.model_secret_join_j4 import (
    EnumJ4Surface,
    ModelJoinJ4Command,
    ModelJoinJ4Result,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemoryExposureSurfaces,
    InMemorySecretProbeStore,
    LocalExposureSurfaces,
)
from omnimarket.nodes.node_secret_reading_effect.adapters.drill_consumers import (
    DRILL_CONSUMERS,
    LeakyLogConsumer,
    LeakyRowConsumer,
)
from omnimarket.nodes.node_secret_reading_effect.handlers.handler_secret_join_j4 import (
    FAILURE_EXPOSED,
    FAILURE_SCAN_BLIND,
    HandlerSecretJoinJ4,
    OverlayKeyConsumer,
    ProtocolJ4Consumer,
)
from omnimarket.nodes.node_secret_reading_effect.overlay_join_j4 import (
    ModelJoinJ4Overlay,
    load_join_overlay,
)
from omnimarket.nodes.node_secret_reading_effect.runtime_join_j4 import (
    _consumer,
    _held,
    _log,
    _log_sink,
)
from tests.nodes.node_secret_reading_effect.secret_reading_support import (
    DUMMY_SURFACE,
    GATE_VALUE,
    HOST,
    MARKER,
    document,
    overlay,
    store,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CODE_SURFACE = "consumer/handler.py"
LOG_FILE = "consumer.log"
CONSUMER_NODE = "node_consumer_effect:resolve_by_overlay"
SCAN_NODE = "node_secret_reading_effect:scan_join_j4"
CORRELATION = "j4-run-1"
NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_secret_reading_effect"
)


def join_overlay(**changes: object) -> ModelJoinJ4Overlay:
    base: dict[str, object] = {
        "consumer": "gate",
        "consumer_node": CONSUMER_NODE,
        "consumer_piece": 4,
        "scan_node": SCAN_NODE,
        "scan_piece": 5,
        "code_globs": [CODE_SURFACE],
        "log_surfaces": [LOG_FILE],
        "max_bytes_per_surface": 4096,
        "emission_journal": "emissions.jsonl",
        "lane": "secret-join-j4",
        "actor": "node:node_secret_reading_effect",
        "drill": False,
        "injected_fault": "Swap the consumer for one that logs its key's value",
    }
    base.update(changes)
    return ModelJoinJ4Overlay.model_validate(base)


def surfaces(**texts: str) -> InMemoryExposureSurfaces:
    base = {
        CODE_SURFACE: "def resolve(handle):\n    return store.resolve(handle)\n",
        LOG_FILE: "an earlier log line naming GATE_PASSWORD by name only\n",
        DUMMY_SURFACE: f"planted {MARKER} here\n",
    }
    base.update(texts)
    return InMemoryExposureSurfaces(base)


def _drop(_line: str) -> None:
    return None


def ticking() -> Callable[[], datetime]:
    state = {"n": 0}

    def tick() -> datetime:
        state["n"] += 1
        return NOW + timedelta(seconds=state["n"])

    return tick


class Run:
    """One join run with everything it touched kept for the scans."""

    def __init__(
        self,
        *,
        consumer: ProtocolJ4Consumer | None = None,
        test_store: InMemorySecretProbeStore | None = None,
        test_surfaces: InMemoryExposureSurfaces | None = None,
        correlation: str = CORRELATION,
        write: bool = True,
        clock: Callable[[], datetime] | None = None,
        sink: InMemoryEmissionSink | None = None,
    ) -> None:
        self.sink = sink or InMemoryEmissionSink()
        self.ledger = InMemoryLedger()
        self.log: list[str] = []
        node = HandlerSecretJoinJ4(
            reading=overlay(),
            join=join_overlay(),
            store=test_store or store(),
            surfaces=test_surfaces or surfaces(),
            sink=self.sink,
            appender=self.ledger,
            consumer=consumer or OverlayKeyConsumer(),
            log=self.log.append,
            clock=clock or ticking(),
            host=HOST,
        )
        self.result: ModelJoinJ4Result = node.handle(
            ModelJoinJ4Command(correlation_id=correlation, write=write)
        )

    def everything(self) -> str:
        """Every text the run produced: result, emissions, log lines, ledger lines."""
        return "\n".join(
            [
                self.result.model_dump_json(),
                *(e.model_dump_json() for e in self.sink.events),
                *self.log,
                *self.ledger.lines,
            ]
        )


def fold(sink: InMemoryEmissionSink) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(minutes=30),
            emissions=tuple(sink.events),
            pieces=(
                ModelPieceDef(piece=4, name="overlays", reading_max_age_s=7200),
                ModelPieceDef(piece=5, name="secrets", reading_max_age_s=7200),
            ),
            joins=(
                ModelJoinDef(
                    id="J4", from_piece=4, to_piece=5, label="overlays to secrets"
                ),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=7200,
            chain_deadline_s=300,
        )
    )


def j4(sink: InMemoryEmissionSink) -> str:
    receipts = fold(sink)
    assert not receipts.findings, [f.code for f in receipts.findings]
    return next(j for j in receipts.joins if j.join == "J4").state


def shape(run: Run) -> list[tuple[int, str, str | None]]:
    return [(e.piece, e.phase, e.failure_code) for e in run.sink.events]


# ------------------------------------------------------------------------------------------------------------ golden


def test_golden_consumer_resolves_by_overlay_and_scan_finds_no_value() -> None:
    run = Run()
    assert run.result.status == "completed"
    assert run.result.verdict is EnumReadingVerdict.WORKING
    assert run.result.scan is not None
    assert run.result.scan.exposed == ()
    assert run.result.scan.dummy_found is True
    assert run.result.scan.code_files == 1
    assert run.result.scan.log_lines == 1
    assert run.result.scan.ledger_rows == 1
    assert GATE_VALUE not in run.everything()


def test_golden_chains_run_in_order_under_one_correlation_id() -> None:
    run = Run()
    assert shape(run) == [
        (4, "start", None),
        (4, "success", None),
        (5, "start", None),
        (5, "success", None),
    ]
    assert {e.correlation_id for e in run.sink.events} == {CORRELATION}
    assert [e.node for e in run.sink.events][::2] == [CONSUMER_NODE, SCAN_NODE]
    assert run.result.consumer_chain == "success"
    assert run.result.scan_chain == "success"


def test_golden_floor_fold_reads_the_join_golden_with_an_age() -> None:
    run = Run()
    receipts = fold(run.sink)
    assert not receipts.findings
    join = next(j for j in receipts.joins if j.join == "J4")
    assert join.state == "golden_only"
    assert join.golden_at is not None
    assert join.error_at is None


def test_golden_the_row_names_the_key_by_name_only_and_lands_on_the_ledger() -> None:
    run = Run()
    assert run.result.ledger == "written"
    assert len(run.ledger.lines) == 1
    row = run.ledger.lines[0]
    assert " | STATUS | lane=secret-join-j4 | src=secret-join-j4 |" in row
    assert "verdict=WORKING" in row
    assert "key=GATE_PASSWORD" in row
    assert "exposed=none" in row
    assert GATE_VALUE not in row


def test_golden_a_probe_run_writes_no_row() -> None:
    run = Run(write=False)
    assert run.ledger.lines == []
    assert run.result.ledger == "not-written"
    assert run.result.verdict is EnumReadingVerdict.WORKING


def test_golden_scan_reads_the_consumers_real_code_files(tmp_path: Path) -> None:
    """Over the node's own files on disk: the scan reads real code and finds no value."""
    planted = tmp_path / "planted.txt"
    planted.write_text(f"planted {MARKER}\n", encoding="utf-8")
    reading = overlay(
        exposure={
            "surfaces": [str(tmp_path / "*.log")],
            "max_bytes_per_surface": 4096,
            "planted_dummy": {"surface": str(planted), "marker": MARKER},
        }
    )
    sink = InMemoryEmissionSink()
    node = HandlerSecretJoinJ4(
        reading=reading,
        join=join_overlay(code_globs=[str(NODE_DIR / "**" / "*.py")], log_surfaces=[]),
        store=store(),
        surfaces=LocalExposureSurfaces(),
        sink=sink,
        appender=InMemoryLedger(),
        consumer=OverlayKeyConsumer(),
        log=_drop,
        clock=ticking(),
        host=HOST,
    )
    result = node.handle(ModelJoinJ4Command(correlation_id=CORRELATION, write=True))
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.scan is not None
    assert result.scan.code_files >= 5


def test_golden_the_example_overlay_is_valid_and_names_no_value() -> None:
    text = (NODE_DIR / "overlay_join_j4.example.yaml").read_text(encoding="utf-8")
    parsed = ModelJoinJ4Overlay.model_validate(yaml.safe_load(text))
    assert parsed.consumer_piece == 4
    assert parsed.scan_piece == 5


# ------------------------------------------------------------------------------------------------------------- error


def test_error_a_value_planted_in_a_log_line_is_found_and_the_join_reads_not_working() -> (
    None
):
    run = Run(consumer=LeakyLogConsumer())
    assert run.result.verdict is EnumReadingVerdict.NOT_WORKING
    assert run.result.scan is not None
    assert run.result.scan.exposed == (EnumJ4Surface.LOG,)
    assert run.result.reasons == (FAILURE_EXPOSED,)
    assert shape(run) == [
        (4, "start", None),
        (4, "failure", FAILURE_EXPOSED),
        (5, "failure", "upstream_" + FAILURE_EXPOSED),
    ]
    assert run.result.scan_chain == "upstream-refusal"


def test_error_control_the_same_run_without_the_break_reads_working() -> None:
    clean = Run()
    broken = Run(consumer=LeakyLogConsumer())
    assert clean.result.verdict is EnumReadingVerdict.WORKING
    assert broken.result.verdict is EnumReadingVerdict.NOT_WORKING
    assert clean.result.scan is not None
    assert broken.result.scan is not None
    assert clean.result.scan.exposed != broken.result.scan.exposed


def test_error_floor_fold_reads_the_join_error_receipt_with_no_finding() -> None:
    run = Run(consumer=LeakyLogConsumer())
    receipts = fold(run.sink)
    assert not receipts.findings
    join = next(j for j in receipts.joins if j.join == "J4")
    assert join.state == "error_only"
    assert join.error_at is not None
    assert join.golden_at is None


def test_error_golden_then_error_under_two_ids_reads_the_join_passed() -> None:
    sink = InMemoryEmissionSink()
    clock = ticking()
    Run(correlation="j4-golden", sink=sink, clock=clock)
    Run(consumer=LeakyLogConsumer(), correlation="j4-error", sink=sink, clock=clock)
    assert j4(sink) == "passed"


def test_error_the_leaking_value_never_reaches_a_row_an_event_or_a_result() -> None:
    for leaky in (LeakyLogConsumer(), LeakyRowConsumer()):
        run = Run(consumer=leaky)
        events_and_rows = "\n".join(
            [
                run.result.model_dump_json(),
                *(e.model_dump_json() for e in run.sink.events),
                *run.ledger.lines,
            ]
        )
        assert GATE_VALUE not in events_and_rows


def test_error_a_value_in_the_ledger_row_cells_is_found_and_not_appended() -> None:
    run = Run(consumer=LeakyRowConsumer())
    assert run.result.verdict is EnumReadingVerdict.NOT_WORKING
    assert run.result.scan is not None
    assert run.result.scan.exposed == (EnumJ4Surface.LEDGER,)
    assert len(run.ledger.lines) == 1
    assert "exposed=ledger" in run.ledger.lines[0]
    assert GATE_VALUE not in run.ledger.lines[0]


def test_error_a_value_in_the_consumers_code_is_found() -> None:
    leaked = surfaces(**{CODE_SURFACE: f"PASSWORD = '{GATE_VALUE}'\n"})
    run = Run(test_surfaces=leaked)
    assert run.result.scan is not None
    assert run.result.scan.exposed == (EnumJ4Surface.CODE,)
    assert run.result.verdict is EnumReadingVerdict.NOT_WORKING
    assert GATE_VALUE not in run.everything()


def test_error_a_value_already_in_the_nodes_log_file_is_found() -> None:
    leaked = surfaces(**{LOG_FILE: f"old line carrying {GATE_VALUE}\n"})
    run = Run(test_surfaces=leaked)
    assert run.result.scan is not None
    assert run.result.scan.exposed == (EnumJ4Surface.LOG,)
    assert run.result.verdict is EnumReadingVerdict.NOT_WORKING


def test_error_a_key_the_store_refuses_ends_the_consumer_in_a_typed_failure() -> None:
    refused = store()
    refused.revoke("gate")
    run = Run(test_store=refused)
    assert run.result.verdict is EnumReadingVerdict.NOT_WORKING
    assert run.result.reasons == ("consumer_key_refused",)
    assert shape(run) == [
        (4, "start", None),
        (4, "failure", "consumer_key_refused"),
        (5, "failure", "upstream_consumer_key_refused"),
    ]
    assert j4(run.sink) == "error_only"


def test_error_a_store_that_is_down_reads_not_working_not_silent() -> None:
    down = store()
    down.down = True
    run = Run(test_store=down)
    assert run.result.verdict is EnumReadingVerdict.NOT_WORKING
    assert run.result.outcome is not None
    assert run.result.outcome.value == "source-unreadable"
    assert run.result.reasons == ("consumer_key_unreachable",)


def test_error_a_scan_that_misses_its_planted_dummy_is_blind_and_never_passes() -> None:
    blind = surfaces(**{DUMMY_SURFACE: "nothing planted here\n"})
    run = Run(test_surfaces=blind)
    assert run.result.verdict is EnumReadingVerdict.NOT_WORKING
    assert run.result.reasons == ("planted-dummy-missed",)
    assert shape(run)[-1] == (5, "failure", FAILURE_SCAN_BLIND)
    receipts = fold(run.sink)
    join = next(j for j in receipts.joins if j.join == "J4")
    assert join.golden_at is None


def test_error_a_surface_that_cannot_be_read_is_not_a_clean_scan() -> None:
    unreadable = surfaces()
    unreadable.unreadable.add(CODE_SURFACE)
    run = Run(test_surfaces=unreadable)
    assert run.result.verdict is EnumReadingVerdict.NOT_WORKING
    assert run.result.outcome is not None
    assert run.result.outcome.value == "source-unreadable"


def test_error_a_consumer_not_declared_in_the_reading_overlay_fails_typed() -> None:
    node = HandlerSecretJoinJ4(
        reading=overlay(),
        join=join_overlay(consumer="nobody"),
        store=store(),
        surfaces=surfaces(),
        sink=InMemoryEmissionSink(),
        appender=InMemoryLedger(),
        consumer=OverlayKeyConsumer(),
        log=_drop,
        clock=ticking(),
        host=HOST,
    )
    result = node.handle(ModelJoinJ4Command(correlation_id=CORRELATION, write=True))
    assert result.status == "failed"
    assert result.failure == "consumer-not-declared:nobody"


def test_error_a_row_that_cannot_be_appended_fails_the_run() -> None:
    sink = InMemoryEmissionSink()
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    node = HandlerSecretJoinJ4(
        reading=overlay(),
        join=join_overlay(),
        store=store(),
        surfaces=surfaces(),
        sink=sink,
        appender=ledger,
        consumer=OverlayKeyConsumer(),
        log=_drop,
        clock=ticking(),
        host=HOST,
    )
    result = node.handle(ModelJoinJ4Command(correlation_id=CORRELATION, write=True))
    assert result.status == "failed"
    assert result.failure == "append-failed:refused"


def test_error_an_overlay_without_a_key_is_refused_naming_it() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelJoinJ4Overlay.model_validate({"consumer": "gate"})
    assert "code_globs" in str(excinfo.value)


def test_error_a_drill_overlay_is_not_evidence_for_the_join() -> None:
    sink = InMemoryEmissionSink()
    node = HandlerSecretJoinJ4(
        reading=overlay(),
        join=join_overlay(drill=True),
        store=store(),
        surfaces=surfaces(),
        sink=sink,
        appender=InMemoryLedger(),
        consumer=LeakyLogConsumer(),
        log=_drop,
        clock=ticking(),
        host=HOST,
    )
    node.handle(ModelJoinJ4Command(correlation_id=CORRELATION, write=False))
    assert all(e.drill for e in sink.events)
    join = next(j for j in fold(sink).joins if j.join == "J4")
    assert join.state == "not_run"


def test_error_an_injected_break_is_refused_outside_a_drill_overlay() -> None:
    assert set(DRILL_CONSUMERS) == {"log-value", "row-value"}
    with pytest.raises(ValueError, match="only under a drill overlay"):
        _consumer("log-value", drill=False)
    assert isinstance(_consumer("log-value", drill=True), LeakyLogConsumer)
    assert isinstance(_consumer(None, drill=False), OverlayKeyConsumer)


def test_error_the_overlay_loader_refuses_an_absent_overlay(tmp_path: Path) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError

    with pytest.raises(NodeOverlayError):
        load_join_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_golden_overlay_loader_reads_a_document_under_a_root(tmp_path: Path) -> None:
    folder = tmp_path / "node_secret_reading_join_j4"
    folder.mkdir()
    text = (NODE_DIR / "overlay_join_j4.example.yaml").read_text(encoding="utf-8")
    (folder / "overlay.yaml").write_text(text, encoding="utf-8")
    loaded = load_join_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert loaded.lane == "secret-join-j4"
    assert document()["store"] == {"secret_store": None}


def test_error_a_drill_log_is_held_in_process_and_goes_nowhere_else(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _log_sink(drill=True) is _held
    assert _log_sink(drill=False) is _log
    _log_sink(drill=True)(f"line carrying {GATE_VALUE}")
    seen = capsys.readouterr()
    assert GATE_VALUE not in seen.out
    assert GATE_VALUE not in seen.err
    _log_sink(drill=False)("a clean operational line")
    assert capsys.readouterr().err == "a clean operational line\n"
