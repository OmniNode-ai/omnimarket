# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the publish and the delegation history node: a step, a file name and a path both sides must agree on.

The publish runs ``node_floor_delegation_history_effect`` as an optional input step beside the delegation tracker and serves the
file it writes. The step's output path is declared twice (the node's contract, the publish's contract) and a rename on one side
alone leaves the file unserved; this test fails when either side changes shape. Golden chain: a cycle serves the history file and
reports it published. Error chain: a cycle with no history file reports it absent and serves none; a refused cycle still serves
the last history file, as it does the tracker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    PublishRefusedError,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
)
from omnimarket.nodes.node_floor_delegation_history_effect.handlers.handler_floor_delegation_history import (
    OWN_CONTRACT,
)
from omnimarket.nodes.node_floor_delegation_history_effect.models.model_floor_delegation_history import (
    ModelHistoryContractRuntime,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS
from tests.nodes.node_factory_floor_publish_effect.test_tracker_served_on_refusal import (
    FloorStepRefusing,
    _home,
    _overlay,
)

pytestmark = pytest.mark.unit

HISTORY = {
    "schema_id": "omnimarket.floor_delegation_history.v1",
    "generated_at": "2026-11-14T02:30:00Z",
}


def _history_home(tmp_path: Path, *, write: bool) -> Path:
    home = _home(tmp_path, previous=None)
    if write:
        target = home / load_runtime().delegation_history_data_relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(HISTORY), encoding="utf-8")
    return home


def _cycle(tmp_path: Path, *, write: bool, refuse: bool = False) -> Path:
    home = _history_home(tmp_path, write=write)
    request = ModelFactoryFloorPublishRequest(
        home=str(home), overlay=_overlay(tmp_path)
    )
    handler = HandlerFactoryFloorPublish(FloorStepRefusing(refuse=refuse))
    if refuse:
        with pytest.raises(PublishRefusedError):
            handler.handle(request, environ={})
    else:
        handler.handle(request, environ={})
    return tmp_path / "site"


def test_the_history_step_is_an_optional_concurrent_input_step_before_the_record_step() -> (
    None
):
    rt = load_runtime().with_limits(TEST_LIMITS)
    names = [s.name for s in rt.steps]
    assert names.index("delegation_history") < names.index(rt.record_step)
    step = next(s for s in rt.steps if s.name == "delegation_history")
    assert step.concurrent
    assert not step.required
    assert sum(rt.declared_longest_path().values()) <= rt.cycle.bound_seconds


def test_the_path_the_node_writes_is_the_path_the_publish_reads() -> None:
    node = ModelHistoryContractRuntime.model_validate(
        yaml.safe_load(OWN_CONTRACT.read_text())["runtime"]
    )
    assert node.output_relpath == load_runtime().delegation_history_data_relpath
    assert load_runtime().names.delegation_history_data == "delegation-history.json"


def test_golden_a_cycle_serves_the_history_file_and_reports_it_published(
    tmp_path: Path,
) -> None:
    site = _cycle(tmp_path, write=True)
    assert json.loads((site / "delegation-history.json").read_text()) == HISTORY
    status = json.loads((site / load_runtime().names.status).read_text())
    assert status["delegation_history"] == "published"


def test_error_a_cycle_with_no_history_file_reports_it_absent_and_serves_none(
    tmp_path: Path,
) -> None:
    site = _cycle(tmp_path, write=False)
    assert not (site / "delegation-history.json").exists()
    status = json.loads((site / load_runtime().names.status).read_text())
    assert status["delegation_history"] == "absent"


def test_error_a_refused_cycle_still_serves_the_last_history_file(
    tmp_path: Path,
) -> None:
    site = _cycle(tmp_path, write=True, refuse=True)
    assert json.loads((site / "delegation-history.json").read_text()) == HISTORY


def test_control_a_publish_contract_without_the_step_is_caught_by_the_step_check() -> (
    None
):
    """The named injected break: the step removed from a copy of the publish contract leaves the check with nothing to find."""
    from omnimarket.nodes.node_factory_floor_publish_effect.handlers import (
        handler_factory_floor_publish as module,
    )

    contract = Path(module.__file__).resolve().parents[1] / "contract.yaml"
    steps = [
        s["name"] for s in yaml.safe_load(contract.read_text())["runtime"]["steps"]
    ]
    assert "delegation_history" in steps
    broken = [name for name in steps if name != "delegation_history"]
    assert "delegation_history" not in broken
