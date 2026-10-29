# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine ladder is the contract field config.engine_ladder, not a constant (OMN-21475, T-S3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.handler_engine_ladder import load_engine_ladder
from omnimarket.models.model_engine_experiment import ModelEngineExperimentRow
from omnimarket.models.model_engine_ladder import CONTEXT_KEY
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLaneAttempt,
    ModelRemoteLaneLadder,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "src/omnimarket/nodes/node_remote_lane_compute/contract.yaml"


def contract_with(tmp_path: Path, *extra: dict[str, str]) -> Path:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    document["config"]["engine_ladder"] = [
        *document["config"]["engine_ladder"],
        *extra,
    ]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def test_the_contract_pairs_each_rung_with_its_host_engine() -> None:
    ladder = load_engine_ladder()
    assert [(e.rung, e.host_engine) for e in ladder.entries] == [
        ("code-edit", "claude_sonnet"),
        ("glm", "claude_sonnet"),
        ("codex", "codex"),
        ("sonnet", "claude_sonnet"),
        ("opus", "claude_opus"),
    ]
    assert ladder == load_engine_ladder(CONTRACT)


def test_no_source_file_holds_the_retired_constant() -> None:
    token = "ENGINE" + "_LADDER"
    holders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src").rglob("*.py")
        if token in path.read_text(encoding="utf-8")
    ]
    assert holders == []


def test_a_rung_the_fixture_contract_adds_is_valid_in_every_model_that_reads_the_ladder(
    tmp_path: Path,
) -> None:
    new = {"rung": "claude-opus-9", "host_engine": "claude_opus"}
    fixture = load_engine_ladder(contract_with(tmp_path, new))
    context = {CONTEXT_KEY: fixture}
    ladder_facts = {"lane": "l-1", "ceiling": "claude-opus-9"}
    attempt = {"engine": "claude-opus-9", "model": "m", "outcome": "done"}
    experiment = {
        "lane": "l-1",
        "attempt": 1,
        "engine": "claude-opus-9",
        "model": "m",
        "outcome": "done",
        "duration_s": 1,
        "next_engine": "claude-opus-9",
    }
    for model, document in (
        (ModelRemoteLaneLadder, ladder_facts),
        (ModelRemoteLaneAttempt, attempt),
        (ModelEngineExperimentRow, experiment),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(document)
        assert model.model_validate(document, context=context)


def test_the_placement_handler_takes_its_rungs_and_host_engines_from_the_ladder(
    tmp_path: Path,
) -> None:
    new = {"rung": "claude-opus-9", "host_engine": "claude_opus"}
    handler = HandlerRemoteLanePlacement(
        load_engine_ladder(contract_with(tmp_path, new))
    )
    assert handler._engines.rungs[-1] == "claude-opus-9"
    assert HandlerRemoteLanePlacement()._engines.rungs[-1] == "opus"
    assert handler._engines.host_engine("claude-opus-9") == "claude_opus"
