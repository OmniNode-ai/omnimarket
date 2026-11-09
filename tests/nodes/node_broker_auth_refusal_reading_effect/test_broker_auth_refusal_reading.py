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
    BURST,
    EXAMPLE,
    HOST,
    NEEDLE,
    World,
    overlay,
)

pytestmark = pytest.mark.unit

COMMAND = ModelBrokerAuthRefusalCommand(correlation_id="c-1", write=True)


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
    assert result.reasons == ("refusals-3-over-0",)
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
    window = summarize([f"no-timestamp {NEEDLE}"], 80)
    assert window.total == 1
    assert window.minutes == ()
    assert window.first_seen == ""


def test_the_sample_is_cut_to_the_overlay_length_and_holds_no_separator() -> None:
    window = summarize([f"2026-11-09T14:57:32Z a|b {NEEDLE} " + "x" * 400], 50)
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
