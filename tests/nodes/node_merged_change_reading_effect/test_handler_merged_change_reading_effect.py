# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect end to end on the in-memory profile: real log lines through the parser, the compute node, the readings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.models.model_merged_change_reading import (
    ModelMcrBuildFact,
    ModelMcrChange,
    ModelMcrRunner,
)
from omnimarket.nodes.node_merged_change_reading_effect.handlers.handler_merged_change_reading_effect import (
    HandlerMergedChangeReadingEffect,
)
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrRunRequest,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_in_memory import (
    SourceInMemoryChangeReading,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_runtime_log import (
    parse_runtime_log,
)
from omnimarket.protocols.protocol_merged_change_sources import (
    ProtocolMergedChangeSources,
)

from .support import LOG_TEXT, config, labels

NOW = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
STARTED = datetime(2026, 11, 14, 8, 56, 27, tzinfo=UTC)

SCHEDULED = ModelMcrRunner(
    repo="omnimarket",
    number=6135,
    kind="schedule",
    nodes=("node_landing_canary_scheduled_effect",),
    trigger_topics=("onex.intent.platform.runtime-tick.v1",),
    result_topics=("onex.evt.omnimarket.landing-canary-completed.v1",),
)
CENSUS = ModelMcrRunner(
    repo="omnimarket",
    number=6120,
    kind="topic",
    nodes=("node_start_path_census_compute",),
    trigger_topics=("onex.cmd.omnimarket.start-path-census-requested.v1",),
    result_topics=("onex.evt.omnimarket.start-path-census-completed.v1",),
)


class _Container:
    def __init__(self, sources: SourceInMemoryChangeReading) -> None:
        self._sources = sources

    def get_service(self, key: Any) -> Any:
        assert key is ProtocolMergedChangeSources
        return self._sources


def _run(sources: SourceInMemoryChangeReading, numbers: list[int]) -> Any:
    handler = HandlerMergedChangeReadingEffect(_Container(sources), config())  # type: ignore[arg-type]
    return handler.handle(
        ModelMcrRunRequest(
            changes=tuple(ModelMcrChange(repo="omnimarket", number=n) for n in numbers),
            now=NOW,
        )
    )


def _facts(*numbers: int) -> tuple[ModelMcrBuildFact, ...]:
    return tuple(
        ModelMcrBuildFact(repo="omnimarket", number=n, in_build=True, source="test")
        for n in numbers
    )


def test_in_memory_source_satisfies_the_protocol() -> None:
    assert isinstance(SourceInMemoryChangeReading(), ProtocolMergedChangeSources)


def test_golden_log_lines_to_readings() -> None:
    log = LOG_TEXT + (
        "\n2026-11-14 09:12:16 [INFO] omnibase_infra.runtime.service_dispatch_result_applier: "
        "Published output event to onex.evt.omnimarket.landing-canary-completed.v1 (correlation_id=aa)"
    )
    sources = SourceInMemoryChangeReading(
        runners=(SCHEDULED, CENSUS),
        build_facts=_facts(6135, 6120),
        build_started_at=STARTED,
        runtime=parse_runtime_log(log, labels()),
        shas={("omnimarket", 6135): "a" * 40},
    )
    result = _run(sources, [6135, 6120])
    first, second = result.reading.readings
    assert (first.verdict, first.missing_step) == ("SEEN_RUNNING", "")
    assert first.merge_sha == "a" * 40
    assert first.last_run_at == datetime(2026, 11, 14, 9, 12, 16, tzinfo=UTC)
    assert first.last_result == "onex.evt.omnimarket.landing-canary-completed.v1"
    # Census is loaded and has no consumer on its request topic: the missing step is the subscription.
    assert (second.verdict, second.missing_step) == (
        "NOT_RUNNING",
        "no_trigger_subscription",
    )
    assert second.nodes[0].loaded is True
    assert result.reading.summary.running == 1
    assert result.reading.summary.not_running == 1
    assert result.runners_read == 2
    assert result.changes_without_sha == 1


def test_error_chain_each_break_reads_its_own_step() -> None:
    sources = SourceInMemoryChangeReading(
        runners=(SCHEDULED, CENSUS),
        build_facts=(
            ModelMcrBuildFact(
                repo="omnimarket", number=6135, in_build=False, source="test"
            ),
        ),
        build_started_at=STARTED,
        runtime=parse_runtime_log(LOG_TEXT, labels()),
    )
    result = _run(sources, [6135, 6120, 7000])
    steps = {r.number: r.missing_step for r in result.reading.readings}
    assert steps == {
        6135: "not_in_build",
        6120: "not_in_build",  # no fact and the repository was read: not in the build
        7000: "no_declared_runner",
    }


def test_unread_build_and_unresolved_sha_read_unread() -> None:
    sources = SourceInMemoryChangeReading(
        runners=(SCHEDULED,),
        build_unread=("omnimarket#6135",),
        build_started_at=STARTED,
        runtime=parse_runtime_log(LOG_TEXT, labels()),
    )
    result = _run(sources, [6135])
    assert result.reading.readings[0].verdict == "UNREAD"
    assert result.changes_without_sha == 1


def test_refuses_without_any_config() -> None:
    handler = HandlerMergedChangeReadingEffect(
        _Container(SourceInMemoryChangeReading())  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="no config"):
        handler.handle(
            ModelMcrRunRequest(changes=(ModelMcrChange(repo="r", number=1),))
        )
