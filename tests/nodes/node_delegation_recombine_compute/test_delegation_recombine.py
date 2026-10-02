# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance tests for recombination and compute purity."""

import ast
import builtins
import inspect
import socket
import subprocess
from itertools import permutations
from pathlib import Path

import pytest

from omnimarket.models import model_delegation_split_recombine
from omnimarket.models.model_delegation_split_recombine import (
    ModelDelegationFinding,
    ModelDelegationRecombineRequest,
    ModelDelegationSplitRequest,
    ModelDelegationUnitAnswer,
)
from omnimarket.nodes.node_delegation_recombine_compute import (
    HandlerDelegationRecombine,
)
from omnimarket.nodes.node_delegation_split_compute import HandlerDelegationSplit

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "task_class", ["review", "code_review", "summarization", "document", "migration"]
)
def test_order_independent(task_class: str) -> None:
    answers = (
        ModelDelegationUnitAnswer(
            unit_id="u2",
            position=2,
            answer="Third",
            findings=(ModelDelegationFinding(path="a.py", line=4, message="Zulu"),),
        ),
        ModelDelegationUnitAnswer(
            unit_id="u0",
            position=0,
            answer="First",
            findings=(
                ModelDelegationFinding(path="a.py", line=4, message="Alpha"),
                ModelDelegationFinding(path="a.py", line=5, message="Other line"),
            ),
        ),
        ModelDelegationUnitAnswer(
            unit_id="u1",
            position=1,
            answer="Second",
            findings=(
                ModelDelegationFinding(path="b.py", line=4, message="Other path"),
            ),
        ),
    )
    handler = HandlerDelegationRecombine()
    wholes = [
        handler.handle(
            ModelDelegationRecombineRequest(
                task_id="task",
                task_class=task_class,
                answers=order,
            )
        )
        for order in permutations(answers)
    ]
    assert all(whole == wholes[0] for whole in wholes)
    assert wholes[0].task_id == "task"
    assert wholes[0].task_class == task_class
    assert wholes[0].answer == "First\n\nSecond\n\nThird"
    assert tuple((f.path, f.line, f.message) for f in wholes[0].findings) == (
        ("a.py", 4, "Alpha"),
        ("a.py", 5, "Other line"),
        ("b.py", 4, "Other path"),
    )


def test_duplicate_unit_identity_is_rejected() -> None:
    answer = ModelDelegationUnitAnswer(unit_id="same", position=0, answer="text")
    with pytest.raises(ValueError, match="unique"):
        ModelDelegationRecombineRequest(
            task_id="task", task_class="document", answers=(answer, answer)
        )


def test_no_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # A closed import boundary also rejects future model clients hidden behind
    # another helper. Only the shared typed models, pydantic and pure stdlib
    # primitives may enter either node's implementation.
    root = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
    sources = [Path(inspect.getfile(model_delegation_split_recombine))]
    for name in ("node_delegation_split_compute", "node_delegation_recombine_compute"):
        sources.extend((root / name).rglob("*.py"))
    allowed = (
        "__future__",
        "typing",
        "enum",
        "re",
        "itertools",
        "pydantic",
        "omnimarket.models.model_delegation_split_recombine",
        "omnimarket.nodes.node_delegation_split_compute.handlers",
        "omnimarket.nodes.node_delegation_recombine_compute.handlers",
    )
    for path in sources:
        source = path.read_text()
        assert "ModelEventEnvelope" not in source
        assert "ModelHandlerOutput" not in source
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            assert all(
                any(
                    name == prefix or name.startswith(prefix + ".")
                    for prefix in allowed
                )
                for name in imports
            )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("COMPUTE attempted I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    split_request = ModelDelegationSplitRequest(
        task_id="task",
        task_class="summarization",
        size_band="large",
        source="# A\na\n# B\nb\n",
    )
    recombine_request = ModelDelegationRecombineRequest(
        task_id="task",
        task_class="summarization",
        answers=(
            ModelDelegationUnitAnswer(unit_id="u0", position=0, answer="A"),
            ModelDelegationUnitAnswer(unit_id="u1", position=1, answer="B"),
        ),
    )
    splitter, recombiner = HandlerDelegationSplit(), HandlerDelegationRecombine()
    assert splitter.handle(split_request) == splitter.handle(split_request)
    assert recombiner.handle(recombine_request) == recombiner.handle(recombine_request)
    assert vars(splitter) == vars(recombiner) == {}
