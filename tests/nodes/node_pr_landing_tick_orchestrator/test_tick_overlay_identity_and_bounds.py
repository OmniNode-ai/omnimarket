# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick orchestrator's consumer group carries the instance identity, and its reads fit its run limit (OMN-22921).

Two orchestrators (the lab host's and the launching Mac's) shared the group landing-tick-orchestrator-cutover on
2026-11-11, so the lab host's older code took and dropped the Mac's records, and a group join that outlived the
unit's 50 s run limit ended the Mac's pass with exit 124. Each test names the break that turns it red.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    build_snapshot_source,
    load_tick_overlay,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_topic_reader import (
    BusTickTopicReader,
    TickJoinTimeoutError,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickOrchestratorOverlay,
    ModelTickTopicSelection,
    resolved_consumer_group,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.runtime_tick import run_once

pytestmark = pytest.mark.unit

BASE = {
    "backend": "redpanda",
    "act_enabled": False,
    "max_snapshots": 20,
    "floor_piece": 9,
    "emission_journal": ".onex_state/floor-simple/emissions.jsonl",
    "bus_lane": "dev",
    "workspace_root": "/ws",
    "consumer_group": "landing-tick-orchestrator-cutover",
    "host_env": "ONEX_HOST_NAME",
    "snapshot_topic": "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1",
    "poll_timeout_s": 30,
    "join_timeout_s": 40,
    "run_limit_s": 90,
    "run_margin_s": 10,
}


def _overlay(**over: object) -> ModelTickOrchestratorOverlay:
    return ModelTickOrchestratorOverlay.model_validate({**BASE, **over})


def test_two_instances_get_two_consumer_groups() -> None:
    overlay = _overlay()
    mac = resolved_consumer_group(overlay, {"ONEX_HOST_NAME": "mac"})
    lab = resolved_consumer_group(overlay, {"ONEX_HOST_NAME": "h053"})
    assert mac == "landing-tick-orchestrator-cutover-mac"
    assert lab == "landing-tick-orchestrator-cutover-h053"
    assert mac != lab


def test_the_selection_the_reader_is_given_carries_the_identity() -> None:
    seen: list[ModelTickTopicSelection] = []

    class _Reader:
        def read(self, max_records: int, timeout_s: float) -> list[str]:
            return []

    def factory(selection: ModelTickTopicSelection) -> _Reader:
        seen.append(selection)
        return _Reader()

    build_snapshot_source(
        load_tick_overlay(explicit=BASE),
        topic_reader_factory=factory,
        environ={"ONEX_HOST_NAME": "h053"},
    )
    assert [s.consumer_group for s in seen] == [
        "landing-tick-orchestrator-cutover-h053"
    ]
    assert seen[0].join_timeout_s == 40


@pytest.mark.parametrize(
    "environ", [{}, {"ONEX_HOST_NAME": ""}, {"ONEX_HOST_NAME": "  "}]
)
def test_error_chain_a_missing_host_identity_is_refused_never_the_shared_group(
    environ: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="ONEX_HOST_NAME"):
        resolved_consumer_group(_overlay(), environ)


def test_error_chain_a_pass_without_the_host_identity_exits_three_and_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONEX_HOST_NAME", raising=False)

    def factory(selection: ModelTickTopicSelection) -> NoReturn:
        raise AssertionError("no topic may be read without the identity")

    code, line = run_once(
        _overlay(emission_journal=str(tmp_path / "e.jsonl")),
        topic_reader_factory=factory,
    )
    assert code == 3
    assert line["status"] == "overlay_refused"
    assert "ONEX_HOST_NAME" in str(line["detail"])


def test_the_overlay_refuses_a_pass_that_cannot_fit_its_run_limit() -> None:
    # the measured break: a 50 s run limit with a 30 s poll and a join that can take longer
    with pytest.raises(ValidationError, match="run_limit_s 50"):
        _overlay(join_timeout_s=40, poll_timeout_s=30, run_limit_s=50, run_margin_s=5)
    fits = _overlay(
        join_timeout_s=30, poll_timeout_s=20, run_limit_s=120, run_margin_s=10
    )
    assert fits.run_limit_s == 120


@pytest.mark.parametrize(
    "missing", ["host_env", "join_timeout_s", "run_limit_s", "run_margin_s"]
)
def test_a_redpanda_overlay_names_every_identity_and_bound_it_needs(
    missing: str,
) -> None:
    values = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ValidationError, match=missing):
        ModelTickOrchestratorOverlay.model_validate(values)


class _Bus:
    def __init__(self, join_s: float) -> None:
        self._join_s = join_s

    async def __aenter__(self) -> _Bus:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def subscribe(
        self, topic: str, on_message: Callable[..., object] | None = None, **kw: object
    ) -> object:
        await asyncio.sleep(self._join_s)

        class _Handle:
            async def stop(self) -> None:
                return None

        return _Handle()


def _selection(join_s: float) -> ModelTickTopicSelection:
    return ModelTickTopicSelection(
        topic="t",
        consumer_group="g-h053",
        join_timeout_s=join_s,
        bus_lane="dev",
        workspace_root="/ws",
    )


def test_error_chain_a_join_that_outlives_its_bound_ends_the_pass_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnimarket.handlers.overlay_store.bus_opener.open_lane_bus",
        lambda **_kw: _Bus(3600),
    )
    with pytest.raises(
        TickJoinTimeoutError, match=r"g-h053 did not join within 0\.05 s"
    ):
        BusTickTopicReader(_selection(0.05)).read(5, 0.01)


def test_control_a_join_inside_its_bound_reads_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnimarket.handlers.overlay_store.bus_opener.open_lane_bus",
        lambda **_kw: _Bus(0.0),
    )
    assert BusTickTopicReader(_selection(5.0)).read(5, 0.05) == []


def test_error_chain_a_pass_whose_join_timed_out_exits_three_source_unread(
    tmp_path: Path,
) -> None:
    class _TimedOut:
        def read(self, max_records: int, timeout_s: float) -> list[str]:
            raise TickJoinTimeoutError("the consumer group g did not join within 40 s")

    code, line = run_once(
        _overlay(emission_journal=str(tmp_path / "e.jsonl")),
        topic_reader_factory=lambda _s: _TimedOut(),
    )
    assert code == 3
    assert line["status"] == "source_unread"
    assert line["detail"] == "TickJoinTimeoutError"
    assert "did not join" in str(line["reason"])
