# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick reducer: pure, a row per pull request, divergence named per pull request (OMN-22921)."""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.landing_tick import (
    TICK_LEVEL_SUBJECT,
    ModelTickReduceInput,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_reducer.handlers import (
    handler_pr_landing_tick_reducer as module,
)
from omnimarket.nodes.node_pr_landing_tick_reducer.handlers.handler_pr_landing_tick_reducer import (
    HandlerPrLandingTickReducer,
)

pytestmark = pytest.mark.unit

NODE = "node_pr_landing_tick_reducer"
REPO = Path(__file__).resolve().parents[3]
SNAPSHOT = ModelTickSnapshot.model_validate_json(
    (REPO / "tests/fixtures/pr_landing_tick_replay/tick_725_omnimarket.json").read_text(
        encoding="utf-8"
    )
)


def test_registered_with_a_pure_reducer_contract() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["entry-points"]["onex.nodes"][NODE]
        == f"omnimarket.nodes.{NODE}"
    )
    package = importlib.import_module(f"omnimarket.nodes.{NODE}")
    contract = yaml.safe_load(
        package.NodePrLandingTickReducer.contract_path.read_text()
    )
    assert contract["node_type"] == "REDUCER_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["metadata"]["related_tickets"] == ["OMN-22921"]


def test_handler_imports_no_io() -> None:
    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "subprocess", "socket", "time", "random", "pathlib"}


def test_one_row_per_pull_request_plus_the_tick_row() -> None:
    out = HandlerPrLandingTickReducer().handle(
        ModelTickReduceInput(decision=SNAPSHOT.decision)
    )
    subjects = [r.subject for r in out.rows]
    assert TICK_LEVEL_SUBJECT in subjects
    assert {g.pr for g in SNAPSHOT.decision.gates} <= set(subjects)
    assert len(subjects) == len(set(subjects))
    assert out.compared is False
    assert out.equal is False


def test_equal_decisions_have_no_divergence() -> None:
    out = HandlerPrLandingTickReducer().handle(
        ModelTickReduceInput(decision=SNAPSHOT.decision, recorded=SNAPSHOT.decision)
    )
    assert out.equal is True
    assert out.divergences == ()


def test_the_same_input_gives_the_same_output() -> None:
    request = ModelTickReduceInput(
        decision=SNAPSHOT.decision, recorded=SNAPSHOT.decision
    )
    handler = HandlerPrLandingTickReducer()
    assert handler.handle(request) == handler.handle(request)


def test_a_changed_tick_level_field_diverges_on_the_tick_row_not_on_a_pull_request() -> (
    None
):
    recorded = SNAPSHOT.decision.model_copy(
        update={"observe_only_refused": ("acme/app",)}
    )
    out = HandlerPrLandingTickReducer().handle(
        ModelTickReduceInput(decision=SNAPSHOT.decision, recorded=recorded)
    )
    assert [d.subject for d in out.divergences] == [TICK_LEVEL_SUBJECT]
