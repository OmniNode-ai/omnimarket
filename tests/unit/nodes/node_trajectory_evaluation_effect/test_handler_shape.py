# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler is definition-B (OMN-20087): inspect plus an AST scan."""

from __future__ import annotations

import ast
import inspect
import types
import typing
from pathlib import Path

import pytest

import omnimarket.nodes.node_trajectory_evaluation_effect as node_pkg
from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.handler_trajectory_evaluation import (
    HandlerTrajectoryEvaluation,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models import (
    model_trajectory_evaluation as m,
)

pytestmark = pytest.mark.unit

_FORBIDDEN = {"ModelEventEnvelope", "ModelHandlerOutput"}


def _members(annotation: object) -> set[object]:
    assert (
        isinstance(annotation, types.UnionType)
        or typing.get_origin(annotation) is typing.Union
    )
    return set(typing.get_args(annotation))


def test_handle_is_async_and_takes_the_command_union() -> None:
    assert inspect.iscoroutinefunction(HandlerTrajectoryEvaluation.handle)
    hints = typing.get_type_hints(HandlerTrajectoryEvaluation.handle)
    assert _members(hints["request"]) == {
        m.ModelTrajectoryEvaluationSubmit,
        m.ModelTrajectoryEvaluationPoll,
    }
    assert _members(hints["return"]) == {
        m.ModelTrajectoryEvaluationAccepted,
        m.ModelTrajectoryEvaluationRefused,
        m.ModelTrajectoryEvaluationFailed,
        m.ModelTrajectoryEvaluationStatus,
    }


def test_no_envelope_no_handler_output_no_plugin_base_in_any_module() -> None:
    root = Path(node_pkg.__file__).parent
    files = sorted(root.rglob("*.py"))
    assert len(files) >= 8
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: set[str] = set()
            if isinstance(node, ast.ImportFrom | ast.Import):
                names = {a.name.split(".")[-1] for a in node.names}
            elif isinstance(node, ast.Name):
                names = {node.id}
            elif isinstance(node, ast.Attribute):
                names = {node.attr}
            elif isinstance(node, ast.ClassDef):
                bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
                assert not any(b.startswith("Plugin") for b in bases), (path, bases)
            assert not names & _FORBIDDEN, (path, names & _FORBIDDEN)
