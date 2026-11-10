# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22699: the publish cycle measures the seconds of every step and phase and writes them where the operator reads them."""

from __future__ import annotations

import itertools
import json
import threading
from pathlib import Path

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
    ModelFactoryFloorPublishResult,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    PLUGIN,
    LocalSsh,
    _home,
    _overlay,
)

STATUS = "publish-status.json"


class TickClock:
    """Every reading advances one second, so a span's seconds count the readings taken inside it."""

    def __init__(self) -> None:
        self._ticks = itertools.count()
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return float(next(self._ticks))


def _run(tmp_path: Path) -> tuple[ModelFactoryFloorPublishResult, Path, Path]:
    home = _home(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    request = ModelFactoryFloorPublishRequest(
        home=str(home), overlay=_overlay(tmp_path), state_dir=str(state)
    )
    result = HandlerFactoryFloorPublish(LocalSsh(), clock=TickClock()).handle(
        request, environ=PLUGIN
    )
    return result, state / load_runtime().evidence_file, tmp_path / "site" / STATUS


def test_every_step_the_phases_and_the_cycle_carry_their_seconds(
    tmp_path: Path,
) -> None:
    result, _, _ = _run(tmp_path)
    steps = [*result.steps, *result.after_publish]
    assert steps
    assert all(s.seconds > 0 for s in steps)
    phases = result.phase_seconds
    assert set(phases) == {
        "input_steps",
        "delegation",
        "copy",
        "history_prune",
        "after_publish",
    }
    assert all(v > 0 for v in phases.values())
    assert result.cycle_seconds >= sum(phases.values())


def test_the_served_status_and_the_heartbeat_name_the_seconds_and_the_slowest_step(
    tmp_path: Path,
) -> None:
    result, evidence, status = _run(tmp_path)
    served = json.loads(status.read_text(encoding="utf-8"))
    runtime = load_runtime()
    assert all(step["seconds"] > 0 for step in served["steps"])
    assert served["cycle_seconds_before_copy"] > 0
    # The status served last names every phase, the after-publish steps, the slowest step and the cycle's total.
    assert set(served["phase_seconds"]) == {
        "input_steps",
        "delegation",
        "copy",
        "history_prune",
        "after_publish",
    }
    assert all(v > 0 for v in served["phase_seconds"].values())
    assert [s["name"] for s in served["after_publish_steps"]] == [
        s.name for s in runtime.after_publish_steps
    ]
    assert all(step["seconds"] > 0 for step in served["after_publish_steps"])
    assert served["cycle_seconds"] == result.cycle_seconds
    assert served["cycle_seconds"] >= sum(served["phase_seconds"].values())
    assert served["cycle_seconds"] > served["cycle_seconds_before_copy"]
    assert served["cycle_bound_seconds"] == runtime.cycle.bound_seconds
    beat = json.loads(evidence.read_text(encoding="utf-8"))
    names = {s.name for s in runtime.steps} | {
        s.name for s in runtime.after_publish_steps
    }
    assert set(beat["step_seconds"]) == names
    assert beat["slowest_step"] in names
    assert served["slowest_step"] == beat["slowest_step"]
    assert beat["step_seconds"][beat["slowest_step"]] == max(
        beat["step_seconds"].values()
    )
    assert beat["cycle_seconds"] > 0
    assert beat["cycle_seconds"] == served["cycle_seconds"]
