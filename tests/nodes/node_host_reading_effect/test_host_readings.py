# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each probe on both backends, each shown failing by a named injected break (OMN-22478)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.models.model_host_reading import (
    EnumHostProbe,
    ModelHostProbeOutcome,
    ModelHostReadingCommand,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.handlers.handler_host_reading import (
    HandlerHostReading,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_host_reading_effect.host_reading_support import (
    LEDGER_HOST,
    RUN_AT,
    WRITING_HOST,
    World,
    overlay,
    queued,
)

pytestmark = pytest.mark.unit


@pytest.fixture(params=["memory", "filesystem"])
def world(request: pytest.FixtureRequest, tmp_path: Path) -> World:
    return World(request.param, tmp_path)


def _read(
    world: World, probe: EnumHostProbe, host: str = LEDGER_HOST
) -> ModelHostProbeOutcome:
    handler = HandlerHostReading(
        overlay=overlay(),
        host=host,
        files=world.files,
        appender=None,
        clock=lambda: RUN_AT,
        environ=world.environ,
    )
    result = handler.handle(
        ModelHostReadingCommand(correlation_id="c", probes=(probe,), write=False)
    )
    assert result.status == "completed", result.failure
    return result.probes[0]


def test_healthy_old_names_read_working(world: World) -> None:
    reading = _read(world, EnumHostProbe.LEDGER_OLD_NAMES)
    assert reading.verdict is EnumReadingVerdict.WORKING
    assert reading.reasons == ("old-names-link-to-the-configured-ledger",)


@pytest.mark.parametrize(
    ("named_break", "reason"),
    [
        (
            World.break_restore_old_name_as_regular_file,
            "old-name-not-a-link-docs-old-ledger-md",
        ),
        (World.break_remove_old_name, "old-name-missing-docs-old-archive"),
        (
            World.break_point_old_name_elsewhere,
            "old-name-wrong-target-docs-old-ledger-md",
        ),
    ],
)
def test_named_break_fails_the_old_names_reading(
    world: World, named_break: Callable[[World], None], reason: str
) -> None:
    named_break(world)
    reading = _read(world, EnumHostProbe.LEDGER_OLD_NAMES)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.outcome is EnumReadingOutcome.READ
    assert reason in reading.reasons


def test_configured_path_that_is_an_old_name_cannot_pass(world: World) -> None:
    """A unit still configured with the old name would compare the link with itself."""
    world.environ["ONEX_LEDGER_PATH"] = str(world.root / "docs/old/LEDGER.md")
    reading = _read(world, EnumHostProbe.LEDGER_OLD_NAMES)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert "configured-path-is-a-link" in reading.reasons


@pytest.mark.parametrize(
    "unset", ["OMNI_HOME", "ONEX_LEDGER_PATH"], ids=["root", "ledger"]
)
def test_unset_route_reads_unknown_not_working(world: World, unset: str) -> None:
    del world.environ[unset]
    reading = _read(world, EnumHostProbe.LEDGER_OLD_NAMES)
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert reading.reasons[0].endswith(unset.lower())


def test_empty_outbox_reads_working(world: World) -> None:
    reading = _read(world, EnumHostProbe.LEDGER_OUTBOX)
    assert reading.verdict is EnumReadingVerdict.WORKING
    assert reading.reasons == ("outbox-within-bounds",)


def test_queued_row_inside_the_bound_reads_working(world: World) -> None:
    world.break_plant_stale_queued_request(age_seconds=900)
    assert (
        _read(world, EnumHostProbe.LEDGER_OUTBOX).verdict is EnumReadingVerdict.WORKING
    )


@pytest.mark.parametrize(
    ("named_break", "reason"),
    [
        (World.break_plant_stale_queued_request, "oldest-over-900s"),
        (World.break_plant_refused_file, "refused-over-0"),
    ],
)
def test_named_break_fails_the_outbox_reading(
    world: World, named_break: Callable[[World], None], reason: str
) -> None:
    named_break(world)
    reading = _read(world, EnumHostProbe.LEDGER_OUTBOX)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reason in reading.reasons


def test_unreadable_queued_file_fails_the_outbox_reading(world: World) -> None:
    world.write(world.outbox / "0001_bad.json", "{not json")
    reading = _read(world, EnumHostProbe.LEDGER_OUTBOX)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert "queued-file-unreadable" in reading.reasons


def test_outbox_cells_name_the_measure(world: World) -> None:
    world.write(world.outbox / "0001_r1.json", queued(1000))
    world.write(world.outbox / "0002_r2.json", queued(10, "r2"))
    world.break_plant_refused_file()
    handler = HandlerHostReading(
        overlay=overlay(),
        host=LEDGER_HOST,
        files=world.files,
        appender=(ledger := InMemoryLedger()),
        clock=lambda: RUN_AT,
        environ=world.environ,
    )
    handler.handle(
        ModelHostReadingCommand(
            correlation_id="c", probes=(EnumHostProbe.LEDGER_OUTBOX,)
        )
    )
    assert "pending=2" in ledger.lines[0]
    assert "oldest_s=1000" in ledger.lines[0]
    assert "refused=1" in ledger.lines[0]


def test_a_writing_host_does_not_read_old_names(world: World) -> None:
    """Those names are links only on the ledger host; the overlay scopes the probe there."""
    handler = HandlerHostReading(
        overlay=overlay(),
        host=WRITING_HOST,
        files=world.files,
        appender=None,
        clock=lambda: RUN_AT,
        environ=world.environ,
    )
    result = handler.handle(ModelHostReadingCommand(correlation_id="c", write=False))
    assert [probe.probe for probe in result.probes] == [EnumHostProbe.LEDGER_OUTBOX]
    refused = handler.handle(
        ModelHostReadingCommand(
            correlation_id="c", probes=(EnumHostProbe.LEDGER_OLD_NAMES,), write=False
        )
    )
    assert refused.status == "failed"
    assert refused.failure == "probes-not-declared:ledger-old-names"
