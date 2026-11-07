# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker load reading: verdicts per named break, the metrics parse on recorded output, and the ssh read.

The golden and error chain event tests are in test_broker_load_chain_events.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from omnimarket.nodes.node_broker_load_reading_effect.adapters.metrics_source import (
    SshBrokerMetrics,
    parse_public_metrics,
)
from omnimarket.nodes.node_broker_load_reading_effect.models.model_broker_load import (
    ModelBrokerLoadCommand,
    ModelBrokerLoadResult,
)
from omnimarket.nodes.node_broker_load_reading_effect.overlay import (
    ModelBrokerMetricsSource,
)
from omnimarket.nodes.node_broker_load_reading_effect.protocols import (
    BrokerMetricsError,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_broker_load_reading_effect.broker_load_support import (
    FIXTURE,
    World,
    overlay,
)

pytestmark = pytest.mark.unit


def _read(world: World, write: bool = False) -> ModelBrokerLoadResult:
    return world.handler(InMemoryLedger() if write else None).handle(
        ModelBrokerLoadCommand(correlation_id="t", write=write)
    )


def test_inside_both_bars_reads_working() -> None:
    result = _read(World())
    assert result.verdict.value == "WORKING"
    assert result.reasons == ("inside-both-bars",)
    assert result.partitions_per_shard == 320
    assert result.partitions_over_bar == 0


def test_partitions_over_the_bar_reads_not_working_and_names_what_would_clear_it() -> (
    None
):
    world = World()
    world.break_partitions_over_the_bar()
    result = _read(world)
    assert result.verdict.value == "NOT_WORKING"
    assert result.reasons == ("partitions-per-shard-2894-over-1000",)
    assert result.shards_needed == 3
    assert result.partitions_over_bar == 1894


def test_slow_consumer_start_reads_not_working() -> None:
    world = World()
    world.break_consumer_start_slow()
    result = _read(world)
    assert result.verdict.value == "NOT_WORKING"
    assert result.reasons == ("consumer-start-21000ms-over-5000ms",)


def test_consumer_that_never_starts_reads_not_working() -> None:
    world = World()
    world.break_consumer_never_starts()
    assert _read(world).reasons == ("consumer-start-timeout",)


def test_both_over_names_both() -> None:
    world = World()
    world.break_partitions_over_the_bar()
    world.break_consumer_never_starts()
    assert len(_read(world).reasons) == 2


def test_unreadable_metrics_read_unknown_never_a_guess() -> None:
    world = World()
    world.break_metrics_unreadable()
    result = _read(world)
    assert result.verdict.value == "UNKNOWN"
    assert result.reasons == ("metrics-unreadable-metrics-unreadable",)
    assert result.metrics is None


def test_unreadable_consumer_transport_reads_unknown_not_slow() -> None:
    world = World()
    world.break_consumer_transport_unreadable()
    result = _read(world)
    assert result.verdict.value == "UNKNOWN"
    assert result.reasons == ("consumer-unreadable-oserror",)


def test_row_carries_the_measured_counts_the_bars_and_the_proposal_cells() -> None:
    world = World()
    world.break_partitions_over_the_bar()
    ledger = InMemoryLedger()
    world.handler(ledger).handle(ModelBrokerLoadCommand(correlation_id="t"))
    row = ledger.lines[0]
    for cell in (
        "lane=broker-load",
        "src=broker-load",
        "verdict=NOT_WORKING",
        "partitions=2894",
        "shards=1",
        "partitions_per_shard=2894",
        "empty_partitions=2400",
        "shards_needed=3",
        "partitions_over_bar=1894",
        "bar_partitions_per_shard=1000",
        "bar_consumer_start_ms=5000",
    ):
        assert cell in row, cell


def test_the_bars_are_in_the_overlay_hash() -> None:
    changed = overlay().model_copy(
        update={
            "bars": overlay().bars.model_copy(update={"max_partitions_per_shard": 500})
        }
    )
    assert changed.overlay_hash() != overlay().overlay_hash()


# --- the metrics parse, on output recorded from the dev broker ---------------------------


def test_recorded_dev_broker_output_parses_to_its_counts() -> None:
    metrics = parse_public_metrics(FIXTURE.read_text(encoding="utf-8"))
    assert metrics.partitions == 2894
    assert metrics.topics == 2858
    assert metrics.shards == 1
    # four user-topic partitions with a log end offset of zero; the internal topic is not counted
    assert metrics.empty_partitions == 4


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "metrics-lack-cluster-counts"),
        (
            "redpanda_cluster_topics{} 3\nredpanda_cluster_partitions{} 3\n",
            "metrics-lack-shards",
        ),
    ],
)
def test_metrics_without_the_counts_are_refused(text: str, reason: str) -> None:
    with pytest.raises(BrokerMetricsError, match=reason):
        parse_public_metrics(text)


# --- the ssh read ------------------------------------------------------------------------

SOURCE = ModelBrokerMetricsSource.model_validate(overlay().metrics_source.model_dump())


class _Run:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[Sequence[str], bytes | None]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, stdin))
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout, b""
        )


def test_ssh_read_asks_the_brokers_own_container_and_parses_the_answer() -> None:
    run = _Run(FIXTURE.read_bytes())
    metrics = SshBrokerMetrics(SOURCE, {SOURCE.ssh_host_env: "host"}, run).read()
    assert metrics.partitions == 2894
    argv, stdin = run.calls[0]
    assert "host" in argv
    assert stdin is not None
    assert SOURCE.compose_project.encode() in stdin
    assert SOURCE.metrics_url.encode() in stdin


@pytest.mark.parametrize(
    ("run", "environ", "reason"),
    [
        (_Run(b"", 255), {SOURCE.ssh_host_env: "h"}, "metrics-read-exit-255"),
        (
            _Run(b"nothing useful"),
            {SOURCE.ssh_host_env: "h"},
            "metrics-lack-cluster-counts",
        ),
        (_Run(FIXTURE.read_bytes()), {}, "ssh-host-variable-unset"),
    ],
)
def test_ssh_read_names_why_it_could_not_say(
    run: _Run, environ: dict[str, str], reason: str
) -> None:
    with pytest.raises(BrokerMetricsError, match=reason):
        SshBrokerMetrics(SOURCE, environ, run).read()
