# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26661 AC2 -- the trial runs with its model endpoint, tools, limits and repository all supplied.

The test supplies the four and nothing else: no overlay root in the environment, no file read or
written under the working directory, no host address. Each reaches the unit that carries the try
(the loop request the lab host runs), including the loop's own wall budget and empty-finish limit,
which an overlay of the host used to decide.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.code_eval_trial.model_code_eval_overlay import (
    ModelCodeEvalRunnerOverlay,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalArm,
    ModelCodeEvalCandidate,
    ModelCodeEvalRunRequest,
)
from omnimarket.models.model_delegated_code_edit import ModelDelegatedCodeEditRequest
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.builders import (
    generation_settings,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.leg import (
    CodeEvalLegConfig,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import ModelLabWorkUnitRequest
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    LocalShellLabWorkExecutor,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    RUN_ID,
    SHA,
    InMemorySuiteReader,
    leg_config,
    make_bus,
    overlay,
)

pytestmark = pytest.mark.unit

ENDPOINT = "supplied-endpoint-7"
REPOSITORY = "supplied-owner/supplied-repo"


def supplied_config(**limits: object) -> CodeEvalLegConfig:
    """The leg's settings with the supplied repository named in the score overlay (its clone root)."""
    base = leg_config(**limits)
    score = base.score_overlay.model_copy(
        update={
            "repo_roots": {REPOSITORY: "/supplied/clone"},
            "protected_paths": {REPOSITORY: ("contracts/",)},
        }
    )
    return dataclasses.replace(base, score_overlay=score)


def test_a_trial_runs_with_endpoint_tools_limits_and_repository_supplied_by_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    bus = make_bus(
        reader=InMemorySuiteReader(),
        config=supplied_config(
            max_turns=17,
            try_timeout_seconds=1234,
            wall_budget_seconds=900,
            max_no_change_finishes=3,
            draft_checks=["uv run python -c pass", "uv run ruff check"],
            need_tools=["uv", "rg"],
        ),
    )
    bus.send(
        ModelCodeEvalRunRequest(
            correlation_id=RUN_ID,
            tenant_id="lab",
            harness_revision="rev-1",
            publish_cells=False,
            candidates=(
                ModelCodeEvalCandidate(
                    repository=REPOSITORY, merge_commit="1" * 40, contract_id="OMN-1000"
                ),
            ),
            arms=(ModelCodeEvalArm(model="control-model", backend_id=ENDPOINT),),
        )
    )
    bus.drain()

    assert bus.units, "the control passed, so a try was placed"
    for unit in bus.units:
        draft = unit.code_draft
        assert draft is not None
        assert unit.repo == REPOSITORY  # repository
        assert unit.commit_sha == SHA
        assert draft.backend_id == ENDPOINT  # model endpoint
        assert unit.need_tools == ["uv", "rg"]  # tools
        assert [c.argv for c in draft.checks] == [
            ("uv", "run", "python", "-c", "pass"),
            ("uv", "run", "ruff", "check"),
        ]
        assert unit.timeout_seconds == 1234  # limits
        assert draft.max_turns == 17
        assert draft.wall_budget_seconds == 900
        assert draft.max_no_change_finishes == 3
    assert bus.closes, "the run closed"
    assert list(tmp_path.iterdir()) == [], "the run needed no file"


def test_the_loop_request_a_host_runs_carries_the_overlays_limits(
    tmp_path: Path,
) -> None:
    bus = make_bus(
        config=supplied_config(wall_budget_seconds=300, max_no_change_finishes=4)
    )
    bus.send(
        ModelCodeEvalRunRequest(
            correlation_id=RUN_ID,
            tenant_id="lab",
            harness_revision="rev-1",
            publish_cells=False,
            candidates=(
                ModelCodeEvalCandidate(
                    repository=REPOSITORY, merge_commit="1" * 40, contract_id="OMN-1000"
                ),
            ),
            arms=(ModelCodeEvalArm(model="m", backend_id=ENDPOINT),),
        )
    )
    bus.drain()
    unit: ModelLabWorkUnitRequest = bus.units[0]
    runner = LocalShellLabWorkExecutor(tmp_path / "work")
    argv = runner._draft_argv(unit, tmp_path / "tree", tmp_path / "state")
    loop_request = ModelDelegatedCodeEditRequest.model_validate_json(
        Path(argv[argv.index("--request") + 1]).read_text(encoding="utf-8")
    )
    assert loop_request.wall_budget_seconds == 300
    assert loop_request.max_no_change_finishes == 4
    assert loop_request.max_turns == 8


def test_an_overlay_that_names_no_loop_limit_leaves_the_request_and_the_settings_hash_as_they_were() -> (
    None
):
    plain = overlay()
    assert plain.wall_budget_seconds is None
    assert plain.max_no_change_finishes is None
    keys = [name for name, _ in generation_settings(plain, "m", "b")]
    assert "wall_budget_seconds" not in keys
    named = overlay(wall_budget_seconds=300, max_no_change_finishes=4)
    pairs = dict(generation_settings(named, "m", "b"))
    assert pairs["wall_budget_seconds"] == "300"
    assert pairs["max_no_change_finishes"] == "4"


def test_a_wall_budget_that_does_not_fit_under_the_try_timeout_is_refused() -> None:
    with pytest.raises(ValidationError, match="wall_budget_seconds"):
        ModelCodeEvalRunnerOverlay.model_validate(
            overlay().model_dump() | {"wall_budget_seconds": 600}
        )
