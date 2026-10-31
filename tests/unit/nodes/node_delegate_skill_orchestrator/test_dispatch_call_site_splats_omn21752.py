# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch call site splats only TypedDict-returning helpers (OMN-21752).

omnibase_infra's always-run keyword-parity check reads this handler's
``self._dispatch_port.dispatch(...)`` call with ``ast`` and never imports it.
It resolves one splat shape: ``**helper(...)`` where ``helper`` is a
module-level ``def`` in this file annotated to return a module-level class
whose only base is ``TypedDict``. A splat of a bare name fails it closed, and
that refusal lands on every omnibase_infra commit and PR, which is what
omnimarket#4653 (OMN-20557) did with ``**session_kwargs``.

The first test holds the call site to that rule from this repository, so a
splat it cannot resolve is red here and not in a downstream repository. The
rest pin the behaviour the helper keeps: the session model reaches a port that
declares it, and a port that does not never sees the keyword.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _session_model_dispatch_kwargs,
)

pytestmark = pytest.mark.unit

_HANDLER_SOURCE = Path(inspect.getfile(handler_delegate_skill))


def _module_level(tree: ast.Module, name: str, kind: type[ast.stmt]) -> ast.stmt | None:
    matches = [
        statement
        for statement in tree.body
        if isinstance(statement, kind) and getattr(statement, "name", None) == name
    ]
    return matches[0] if len(matches) == 1 else None


def _splat_problem(tree: ast.Module, value: ast.expr) -> str | None:
    """Why the parity reader cannot resolve this splat, or None when it can."""
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
        return "not a call to a named helper"
    helper = _module_level(tree, value.func.id, ast.FunctionDef)
    if not isinstance(helper, ast.FunctionDef):
        return f"{value.func.id} is not exactly one module-level def"
    annotation = helper.returns
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        annotated: str | None = annotation.value.strip()
    elif isinstance(annotation, ast.Name):
        annotated = annotation.id
    else:
        annotated = None
    if not annotated:
        return f"{value.func.id} has no plain-name return annotation"
    typed_dict = _module_level(tree, annotated, ast.ClassDef)
    if not isinstance(typed_dict, ast.ClassDef):
        return f"{annotated} is not exactly one module-level class"
    base = typed_dict.bases[0] if len(typed_dict.bases) == 1 else None
    is_typed_dict = (isinstance(base, ast.Name) and base.id == "TypedDict") or (
        isinstance(base, ast.Attribute) and base.attr == "TypedDict"
    )
    if not is_typed_dict:
        return f"{annotated}'s only base is not TypedDict"
    return None


def _dispatch_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dispatch"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_dispatch_port"
        ):
            calls.append(node)
    return calls


def test_dispatch_call_site_splats_only_typed_dict_returning_helpers() -> None:
    tree = ast.parse(_HANDLER_SOURCE.read_text(encoding="utf-8"))
    calls = _dispatch_calls(tree)
    assert calls, "no self._dispatch_port.dispatch(...) call found in the handler"
    problems = [
        f"line {keyword.value.lineno}: {problem}"
        for call in calls
        for keyword in call.keywords
        if keyword.arg is None
        and (problem := _splat_problem(tree, keyword.value)) is not None
    ]
    assert not problems, (
        "the dispatch call splats something omnibase_infra's keyword-parity "
        "check cannot resolve statically, which refuses every omnibase_infra "
        "commit and PR (OMN-21752): " + "; ".join(problems)
    )


def test_the_splat_rule_refuses_a_bare_name() -> None:
    """Positive control: the rule above is red on the shape that broke."""
    source = (
        "from typing import TypedDict\n"
        "class _K(TypedDict, total=False):\n"
        "    a: int\n"
        "def _h() -> _K:\n"
        "    return {}\n"
    )
    tree = ast.parse(source)
    bare = ast.parse("f(**kwargs)").body[0]
    assert isinstance(bare, ast.Expr)
    call = bare.value
    assert isinstance(call, ast.Call)
    assert _splat_problem(tree, call.keywords[0].value) is not None
    helper_expr = ast.parse("f(**_h())").body[0]
    assert isinstance(helper_expr, ast.Expr)
    helper_call = helper_expr.value
    assert isinstance(helper_call, ast.Call)
    assert _splat_problem(tree, helper_call.keywords[0].value) is None


def _request(**metadata: str) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="summarize the diff",
        task_type="document",
        source="claude-code",
        metadata=metadata,
    )


async def _port_declaring_session_model(*, prompt: str, session_model: str) -> None:
    del prompt, session_model


async def _port_without_session_model(*, prompt: str) -> None:
    del prompt


async def _port_taking_kwargs(*, prompt: str, **extra: object) -> None:
    del prompt, extra


def test_session_model_reaches_a_port_that_declares_it() -> None:
    request = _request(session_model="claude-opus-5-5")
    assert _session_model_dispatch_kwargs(request, _port_declaring_session_model) == {
        "session_model": "claude-opus-5-5"
    }
    assert _session_model_dispatch_kwargs(request, _port_taking_kwargs) == {
        "session_model": "claude-opus-5-5"
    }


def test_session_model_is_withheld_from_a_port_that_does_not_declare_it() -> None:
    request = _request(session_model="claude-opus-5-5")
    assert _session_model_dispatch_kwargs(request, _port_without_session_model) == {}


def test_no_session_model_passes_no_keyword() -> None:
    assert (
        _session_model_dispatch_kwargs(_request(), _port_declaring_session_model) == {}
    )
