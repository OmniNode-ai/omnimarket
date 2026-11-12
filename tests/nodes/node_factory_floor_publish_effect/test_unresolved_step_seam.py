# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998, OMN-23606: the unresolved reads are their own publish step, and the publisher, the step and the floor agree on the seam.

Measured on the launching Mac 2026-11-10 (cProfile, three runs each): the factory floor step took 231 s against its 30 s limit,
207 s of it five ledger reads in series (the window read and four unresolved reads). Seams pinned here, each failing when either side
changes shape: the step's module and record path against the floor's contract, the step's freshness against the floor's age bound,
and the floor step's limit and the longest declared path against the cycle bound, which stays 240 s.
"""

from __future__ import annotations

import importlib
import math
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    OWN_CONTRACT_PATH,
    load_contract,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    group_steps,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect import (
    runtime_factory_floor_unresolved,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.models.model_factory_floor_unresolved import (
    ModelFactoryFloorUnresolvedOverlay,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS

pytestmark = pytest.mark.unit

RT = load_runtime().with_limits(TEST_LIMITS)
FLOOR = load_contract(OWN_CONTRACT_PATH).runtime
STEP = next(s for s in RT.steps if s.name == "factory_floor_unresolved")
RECORD = next(s for s in RT.steps if s.name == RT.record_step)
# Measured 2026-11-10 on the publishing Mac at load 10 to 11 (the file source named, the unresolved record in place, 322,088 window
# rows): three runs of the floor step, 19.6 s, 19.5 s and 19.1 s; and of the unresolved step with 4 workers, 10.6 s, 10.4 s and 10.8 s.
MEASURED_FLOOR_STEP_SECONDS = 19.6
MEASURED_UNRESOLVED_STEP_SECONDS = 10.8
CYCLE_BOUND_SECONDS = TEST_LIMITS.cycle_bound_seconds


def test_the_step_runs_the_unresolved_node_before_the_record_and_is_not_required() -> (
    None
):
    names = [s.name for s in RT.steps]
    assert names.index(STEP.name) < names.index(RT.record_step)
    assert not STEP.required
    assert STEP.concurrent
    # The module the publisher runs exists and is the node whose record the floor reads.
    assert STEP.module is not None
    runtime = importlib.import_module(f"{STEP.module}.runtime_factory_floor_unresolved")
    assert callable(runtime.main)
    assert STEP.args == ("--home", "{home}")


def test_the_step_skips_while_its_record_is_fresh_and_names_the_floors_file() -> None:
    # Break: a fresh_relpath other than the floor's source path would rerun (or never rerun) the step against the wrong file.
    assert STEP.fresh_relpath == FLOOR.sources.unresolved_rows_relpath
    assert STEP.fresh_seconds is not None
    assert STEP.fresh_seconds <= FLOOR.unresolved_max_age_hours * 3600


def test_the_step_costs_the_cycle_nothing_past_its_group() -> None:
    group = next(g for g in group_steps(RT.steps) if STEP in g)
    assert STEP.timeout_seconds == max(s.timeout_seconds for s in group)


def test_the_floor_step_has_the_measured_time_plus_headroom_under_the_unchanged_bound() -> (
    None
):
    """Break: a floor limit raised to pay for slow reads (it was 75 s under a 285 s bound) fails here, not on the served page."""
    assert RECORD.required
    assert RECORD.timeout_seconds >= MEASURED_FLOOR_STEP_SECONDS * 1.5
    assert STEP.timeout_seconds >= MEASURED_UNRESOLVED_STEP_SECONDS * 1.5
    assert RT.cycle.bound_seconds == CYCLE_BOUND_SECONDS
    path = RT.declared_longest_path()
    assert sum(path.values()) <= CYCLE_BOUND_SECONDS, path


def _reads_bound_seconds(overlay: ModelFactoryFloorUnresolvedOverlay) -> int:
    """The longest the step's reads may take: each batch of read_workers reads at one read's bound, one batch after the other."""
    return (
        math.ceil(len(overlay.reads) / overlay.read_workers)
        * overlay.read_timeout_seconds
    )


def test_the_example_overlays_reads_end_inside_the_publishers_step_limit() -> None:
    """Break: the same reads one at a time (read_workers 1) may take four read bounds, past the limit the publisher kills the step at,
    and a killed step closes no chain. The reads' own bounds must end first, so a hung read is the step's typed failure."""
    example = ModelFactoryFloorUnresolvedOverlay.model_validate(
        yaml.safe_load(
            (
                Path(runtime_factory_floor_unresolved.__file__).parent
                / "overlay.example.yaml"
            ).read_text(encoding="utf-8")
        )
    )
    assert _reads_bound_seconds(example) < STEP.timeout_seconds
    serial = example.model_copy(update={"read_workers": 1})
    assert _reads_bound_seconds(serial) > STEP.timeout_seconds
    assert example.overlap_hours >= FLOOR.unresolved_max_age_hours
