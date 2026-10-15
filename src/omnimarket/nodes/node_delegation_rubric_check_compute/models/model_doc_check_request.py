# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied documentation draw: the answer text and the original function."""

import ast
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOC_CHECK_FUNCTION = "retry_delay"

# The reference is executed to grade doctests, so its function is held to a
# straight-line arithmetic subset: no attributes, subscripts, imports, loops,
# nested scopes or calls beyond these names.
REFERENCE_CALLABLES = frozenset(
    {"round", "min", "max", "abs", "float", "int", "ValueError"}
)
_ANNOTATION_NAMES = frozenset({"int", "float", "bool", "str"})
_BODY_NODES = (
    ast.If,
    ast.Raise,
    ast.Return,
    ast.Assign,
    ast.AugAssign,
    ast.Pass,
    ast.Expr,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.Call,
    ast.keyword,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.IfExp,
)


def _numeric_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        node = node.operand
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    )


def _annotation_ok(node: ast.expr | None) -> bool:
    return node is None or (isinstance(node, ast.Name) and node.id in _ANNOTATION_NAMES)


def _power_ok(node: ast.BinOp, params: set[str]) -> bool:
    """An exponent may only use literals and parameters, never another power."""
    for part in ast.walk(node.right):
        if isinstance(part, ast.BinOp) and isinstance(part.op, ast.Pow):
            return False
        if isinstance(part, ast.Name) and part.id not in params:
            return False
    return not any(
        isinstance(part, ast.BinOp) and isinstance(part.op, ast.Pow)
        for part in ast.walk(node.left)
    )


def reference_problem(source: str) -> str | None:
    """Why source is not a safe reference function, or None when it is."""
    try:
        body = ast.parse(source).body
    except SyntaxError as exc:
        return f"reference_source does not parse: {exc.msg}"
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef):
        return "reference_source must be exactly one function definition"
    function = body[0]
    if function.name != DOC_CHECK_FUNCTION:
        return f"reference_source must define {DOC_CHECK_FUNCTION}"
    if function.decorator_list:
        return "reference_source function must not be decorated"
    args = function.args
    if args.vararg or args.kwarg or args.posonlyargs or args.kwonlyargs:
        return "reference_source function must take plain positional parameters"
    if not all(_numeric_literal(d) for d in args.defaults):
        return "reference_source defaults must be numeric literals"
    if not all(_annotation_ok(a.annotation) for a in args.args) or not _annotation_ok(
        function.returns
    ):
        return "reference_source annotations must be plain int, float, bool or str"
    if function.type_params:
        return "reference_source function must not be generic"
    params = {a.arg for a in args.args}
    locals_ = {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign | ast.AugAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    allowed_names = params | locals_ | REFERENCE_CALLABLES
    for statement in function.body:
        for node in ast.walk(statement):
            if not isinstance(node, _BODY_NODES):
                return f"reference_source body may not use {type(node).__name__}"
            if isinstance(node, ast.Name) and (
                node.id not in allowed_names or node.id.startswith("__")
            ):
                return f"reference_source body may not name {node.id}"
            if isinstance(node, ast.Assign | ast.AugAssign) and not all(
                isinstance(t, ast.Name)
                for t in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            ):
                return "reference_source body may only assign plain names"
            if isinstance(node, ast.Call) and not (
                isinstance(node.func, ast.Name) and node.func.id in REFERENCE_CALLABLES
            ):
                return "reference_source body may only call " + ", ".join(
                    sorted(REFERENCE_CALLABLES)
                )
            if isinstance(node, ast.keyword) and node.arg is None:
                return "reference_source body may not unpack keywords"
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Pow)
                and not _power_ok(node, params)
            ):
                return "reference_source exponents may only use literals and parameters"
    return None


class ModelDocCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["delegation_doc_check"] = "delegation_doc_check"
    answer_text: str = Field(min_length=1)
    reference_source: str = Field(min_length=1)
    run_directory: str = Field(min_length=1)

    @field_validator("reference_source")
    @classmethod
    def _reference_is_the_original_function(cls, value: str) -> str:
        """The reference is the walker's own original: one function in the safe subset."""
        problem = reference_problem(value)
        if problem:
            raise ValueError(problem)
        return value
