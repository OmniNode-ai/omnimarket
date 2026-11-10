# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker authentication refusal reading: verdicts, the row, the overlay and the ssh adapter (OMN-19044, AC-4)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.adapters.broker_log import (
    SshBrokerLog,
    SshControlRuns,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.handlers.handler_broker_auth_refusal import (
    summarize,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.models.model_broker_auth_refusal import (
    ModelBrokerAuthRefusalCommand,
    ModelBrokerAuthRefusalResult,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.overlay import (
    ModelBrokerAuthRefusalOverlay,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.protocols import (
    BrokerLogError,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_broker_auth_refusal_reading_effect.auth_refusal_support import (
    AUTH_FAILED_LINE,
    BURST,
    CONTROL_LINE,
    CONTROL_RUN,
    EXAMPLE,
    HOST,
    NEEDLE,
    RUN_AT,
    World,
    overlay,
)

pytestmark = pytest.mark.unit

COMMAND = ModelBrokerAuthRefusalCommand(correlation_id="c-1", write=True)
FORMS = overlay().reading.forms


def _run(world: World, ledger: InMemoryLedger | None) -> ModelBrokerAuthRefusalResult:
    return world.handler(ledger).handle(COMMAND)


def test_a_clean_log_reads_working_and_writes_one_row() -> None:
    ledger = InMemoryLedger()
    result = _run(World(), ledger)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert result.window is not None
    assert result.window.total == 0
    assert result.reasons == ("inside-the-bar",)
    assert len(ledger.lines) == 1
    assert "verdict=WORKING" in ledger.lines[0]
    assert "refusals=0" in ledger.lines[0]
    assert "first_seen=none" in ledger.lines[0]


def test_the_observed_burst_reads_not_working_with_count_times_and_sample() -> None:
    world = World()
    world.break_client_refused_in_a_burst()
    ledger = InMemoryLedger()
    result = _run(world, ledger)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert result.reasons == ("other-refusals-3-over-0",)
    window = result.window
    assert window is not None
    assert window.total == 3
    assert [(m.minute, m.count) for m in window.minutes] == [("2026-11-09T14:57", 3)]
    assert window.first_seen == "2026-11-09T14:57:32"
    assert window.last_seen == "2026-11-09T14:57:35"
    assert "Unexpected auth request 3 expected handshake" in window.sample
    row = ledger.lines[0]
    assert "verdict=NOT_WORKING" in row
    assert "refusals=3" in row
    assert "minutes_with_refusals=1" in row
    assert "first_seen=2026-11-09T14:57:32" in row
    assert "lane=broker-auth-refusal" in row
    assert "sample=" in row


def test_a_count_at_the_bar_reads_working() -> None:
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    config["bars"]["max_refusals"] = 3
    world = World()
    world.break_client_refused_in_a_burst()
    handler = world.handler(None)
    handler._overlay = ModelBrokerAuthRefusalOverlay.model_validate(config)
    result = handler.handle(
        ModelBrokerAuthRefusalCommand(correlation_id="c", write=False)
    )
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.window is not None
    assert result.window.total == 3


def test_an_unreadable_log_reads_unknown_with_a_typed_reason() -> None:
    world = World()
    world.break_log_unreadable()
    ledger = InMemoryLedger()
    result = _run(world, ledger)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert result.failed
    assert "log-unreadable" in ledger.lines[0]
    assert "refusals=unknown" in ledger.lines[0]


def test_a_probe_appends_nothing() -> None:
    world = World()
    world.break_client_refused_in_a_burst()
    ledger = InMemoryLedger()
    result = world.handler(ledger).handle(
        ModelBrokerAuthRefusalCommand(correlation_id="c", write=False)
    )
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert ledger.lines == []


def test_the_row_key_is_stable_for_one_host_time_and_overlay() -> None:
    first = _run(World(), InMemoryLedger())
    second = _run(World(), InMemoryLedger())
    assert first.row_key
    assert first.row_key == second.row_key


def test_a_line_with_no_readable_time_still_counts() -> None:
    window = summarize([f"no-timestamp {NEEDLE}"], sample_max_chars=80, forms=FORMS)
    assert window.total == 1
    assert window.other == 1
    assert window.minutes == ()
    assert window.first_seen == ""


def test_the_sample_is_cut_to_the_overlay_length_and_holds_no_separator() -> None:
    window = summarize(
        [f"2026-11-09T14:57:32Z a|b {NEEDLE} " + "x" * 400],
        sample_max_chars=50,
        forms=FORMS,
    )
    assert len(window.sample) == 50
    assert "|" not in window.sample


def test_an_overlay_missing_a_key_is_refused() -> None:
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del config["bars"]
    with pytest.raises(ValidationError):
        ModelBrokerAuthRefusalOverlay.model_validate(config)


def test_the_overlay_hash_changes_with_the_bar() -> None:
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    config["bars"]["max_refusals"] = 7
    assert (
        ModelBrokerAuthRefusalOverlay.model_validate(config).overlay_hash()
        != overlay().overlay_hash()
    )


class _Runner:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[tuple[Sequence[str], str]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), (stdin or b"").decode()))
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout.encode(), b""
        )


def test_the_ssh_backend_reads_the_container_log_and_keeps_the_matching_lines() -> None:
    runner = _Runner(0, "\n".join(BURST) + "\n")
    log = SshBrokerLog(
        overlay().log_source, {"EXAMPLE_BROKER_SSH_HOST": "broker-host"}, runner
    )
    lines = log.matching_lines(NEEDLE, 600)
    assert lines == BURST
    argv, script = runner.calls[0]
    assert argv[-2:] == ["broker-host", "bash -s"]
    assert "docker logs --since 600s -t" in script
    assert f"grep -F -- {NEEDLE}" in script


@pytest.mark.parametrize(
    ("returncode", "reason"), [(1, "log-read-exit-1"), (255, "log-read-exit-255")]
)
def test_the_ssh_backend_names_a_failed_read(returncode: int, reason: str) -> None:
    log = SshBrokerLog(
        overlay().log_source,
        {"EXAMPLE_BROKER_SSH_HOST": "broker-host"},
        _Runner(returncode),
    )
    with pytest.raises(BrokerLogError, match=reason):
        log.matching_lines(NEEDLE, 600)


def test_the_ssh_backend_refuses_an_unset_host_variable() -> None:
    log = SshBrokerLog(overlay().log_source, {}, _Runner(0))
    with pytest.raises(BrokerLogError, match="ssh-host-variable-unset"):
        log.matching_lines(NEEDLE, 600)


def test_host_constant_matches_the_row() -> None:
    ledger = InMemoryLedger()
    _run(World(), ledger)
    assert f"host={HOST}" in ledger.lines[0]


# ---- the deploy's negative control, told apart from every other client (OMN-19044, AC-4) ----


def test_todays_control_refusal_is_counted_as_the_control_and_does_not_alarm() -> None:
    world = World()
    world.deploy_runs_the_control()
    ledger = InMemoryLedger()
    result = _run(world, ledger)
    assert result.verdict is EnumReadingVerdict.WORKING
    window = result.window
    assert window is not None
    assert (window.total, window.control, window.other) == (1, 1, 0)
    assert window.control_first_seen == "2026-11-09T15:32:43"
    assert window.control_runs == 1
    row = ledger.lines[0]
    assert "verdict=WORKING" in row
    assert "control_refusals=1" in row
    assert "other_refusals=0" in row
    assert "control_last_seen=2026-11-09T15:32:43" in row


@pytest.mark.parametrize(
    "breaker", ["break_control_marker_is_lost", "break_control_marker_is_skewed"]
)
def test_failing_control_the_same_refusal_without_its_marker_alarms(
    breaker: str,
) -> None:
    """The failing control: replay the 15:32:43Z refusal with its marker broken; it must read NOT_WORKING."""
    world = World()
    getattr(world, breaker)()
    result = _run(world, InMemoryLedger())
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons == ("other-refusals-1-over-0",)
    assert result.window is not None
    assert (result.window.control, result.window.other) == (0, 1)


def test_a_second_client_inside_the_control_span_is_another_client() -> None:
    world = World()
    world.break_another_client_during_the_control()
    result = _run(world, InMemoryLedger())
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.window is not None
    assert (result.window.control, result.window.other) == (1, 1)
    assert result.window.first_seen == "2026-11-09T15:32:43"


def test_controls_are_counted_per_run_and_only_the_others_alarm() -> None:
    world = World()
    world.deploy_runs_the_control()
    world.lines = (CONTROL_LINE, AUTH_FAILED_LINE, *BURST, *world.broker_logged[1:])
    result = _run(world, InMemoryLedger())
    window = result.window
    assert window is not None
    assert (window.total, window.control, window.other) == (5, 1, 4)
    assert result.reasons == ("other-refusals-4-over-0",)
    assert {(f.form, f.count) for f in window.forms} == {
        ("handshake-skipped", 3),
        ("auth-failed", 1),
    }
    assert "other_forms=auth-failed:1,handshake-skipped:3" in (
        _run(world, InMemoryLedger()).row or ""
    )


def test_a_control_run_explains_only_the_overlay_count() -> None:
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    config["control"]["max_lines_per_run"] = 2
    world = World()
    world.break_another_client_during_the_control()
    handler = world.handler(None)
    handler._overlay = ModelBrokerAuthRefusalOverlay.model_validate(config)
    result = handler.handle(
        ModelBrokerAuthRefusalCommand(correlation_id="c", write=False)
    )
    assert result.window is not None
    assert (result.window.control, result.window.other) == (2, 0)


def test_an_unreadable_control_record_reads_unknown_not_working() -> None:
    world = World()
    world.break_client_refused_in_a_burst()
    world.break_control_unreadable()
    result = _run(world, InMemoryLedger())
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert "control-unreadable" in result.reasons[0]


def test_the_overlay_hash_changes_with_the_control_marker() -> None:
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    config["control"]["tolerance_seconds"] = 9
    assert (
        ModelBrokerAuthRefusalOverlay.model_validate(config).overlay_hash()
        != overlay().overlay_hash()
    )


def test_the_ssh_control_reader_reads_the_container_span() -> None:
    runner = _Runner(
        0, "2026-11-09T15:32:43.298302438Z 2026-11-09T15:32:44.065886621Z\n"
    )
    runs = SshControlRuns(
        overlay().log_source,
        overlay().control,
        {"EXAMPLE_BROKER_SSH_HOST": "broker-host"},
        runner,
    ).runs()
    assert runs == (CONTROL_RUN,)
    assert (
        "docker inspect --format '{{.State.StartedAt}} {{.State.FinishedAt}}' example-project-control"
        in runner.calls[0][1]
    )


def test_the_ssh_control_reader_gives_no_run_for_an_absent_container() -> None:
    runs = SshControlRuns(
        overlay().log_source,
        overlay().control,
        {"EXAMPLE_BROKER_SSH_HOST": "broker-host"},
        _Runner(0, ""),
    ).runs()
    assert runs == ()


def test_a_running_control_spans_to_now() -> None:
    runner = _Runner(0, "2026-11-09T15:32:43.298302438Z 0001-01-01T00:00:00Z\n")
    runs = SshControlRuns(
        overlay().log_source,
        overlay().control,
        {"EXAMPLE_BROKER_SSH_HOST": "broker-host"},
        runner,
        clock=lambda: RUN_AT.replace(hour=16),
    ).runs()
    assert runs[0].started_at == CONTROL_RUN.started_at
    assert runs[0].finished_at == RUN_AT.replace(hour=16)


def test_the_ssh_control_reader_names_a_failed_read() -> None:
    reader = SshControlRuns(
        overlay().log_source,
        overlay().control,
        {"EXAMPLE_BROKER_SSH_HOST": "broker-host"},
        _Runner(255),
    )
    with pytest.raises(BrokerLogError, match="control-read-exit-255"):
        reader.runs()
