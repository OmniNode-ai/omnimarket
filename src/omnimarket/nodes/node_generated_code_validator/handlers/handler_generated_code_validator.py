"""Pure deterministic validation of a generated ONEX code artifact — no I/O.

Given the artifact source text (and an optional expected structure) the handler
returns typed diagnostics over three deterministic checks:

* **parses** — the source is valid Python.
* **stub bodies** — any function/method (sync *or* async — a generated ``handle``
  is sync) whose body is empty (bare ``pass`` / ``...`` / docstring-only),
  raises ``NotImplementedError``, or carries a stub comment marker.
* **structure** — when an expected structure is supplied, the target class is
  present, inherits the expected base (archetype), and defines the required
  methods (handler signature).

The stub-detection logic is *re-derived* here (never an in-process call to
``node_stub_detector``) and broadened to cover sync methods, per rule 7a. All
work is on the in-memory text: no disk, bus, or DB access.
"""

from __future__ import annotations

import ast
import io
import tokenize

from omnimarket.nodes.node_generated_code_validator.models.model_generated_code_validation import (
    ModelGeneratedCodeValidation,
)
from omnimarket.nodes.node_generated_code_validator.models.model_generated_code_validator_request import (
    ModelExpectedStructure,
    ModelGeneratedCodeValidatorRequest,
)

# Stub comment needles. The marker text lives only in these string constants —
# never in a bare code comment — so the repo's untracked-marker gate does not
# flag this module as carrying an unimplemented marker of its own.
_STUB_COMMENT_NEEDLES: tuple[str, ...] = (
    "IMPLEMENTATION REQUIRED",
    "TODO",
    "FIXME",
)

_FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


def _comment_lines(source_text: str) -> dict[int, list[str]]:
    """Map 1-based line number -> comment token strings on that line."""
    comments: dict[int, list[str]] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source_text).readline):
            if tok.type == tokenize.COMMENT:
                comments.setdefault(tok.start[0], []).append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Tokenizer errors are non-fatal — the AST pass still finds raise markers.
        return comments
    return comments


def _raises_not_implemented(func: _FunctionDef) -> bool:
    """True when the function body contains a ``raise NotImplementedError`` node."""
    for node in ast.walk(func):
        if isinstance(node, ast.Raise):
            exc = node.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
            if isinstance(exc, ast.Attribute) and exc.attr == "NotImplementedError":
                return True
    return False


def _has_stub_comment(func: _FunctionDef, comments: dict[int, list[str]]) -> bool:
    """True when a stub comment needle appears in the function's line range."""
    start = func.lineno
    end = func.end_lineno or func.lineno
    in_range = "\n".join(
        text
        for lineno, texts in comments.items()
        if start <= lineno <= end
        for text in texts
    )
    return any(needle in in_range for needle in _STUB_COMMENT_NEEDLES)


def _body_after_docstring(func: _FunctionDef) -> list[ast.stmt]:
    """Function body with a leading string-literal docstring stripped."""
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return list(body)


def _is_empty_body(body: list[ast.stmt]) -> bool:
    """True when the (post-docstring) body is empty, a bare ``pass``, or ``...``."""
    if not body:
        return True
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    )


def _function_is_stub(func: _FunctionDef, comments: dict[int, list[str]]) -> bool:
    """True when the function is an unimplemented stub."""
    if _is_empty_body(_body_after_docstring(func)):
        return True
    if _raises_not_implemented(func):
        return True
    return _has_stub_comment(func, comments)


def _detect_stub_methods(
    tree: ast.AST, comments: dict[int, list[str]]
) -> tuple[str, ...]:
    """Return the names of every stub function/method, in source order."""
    return tuple(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _function_is_stub(node, comments)
    )


def _base_names(class_def: ast.ClassDef) -> set[str]:
    """Names of the class's declared bases (both ``Name`` and ``Attribute``)."""
    names: set[str] = set()
    for base in class_def.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _check_structure(
    tree: ast.AST, expected: ModelExpectedStructure
) -> tuple[str, ...]:
    """Compare the parsed module against an expected structure."""
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    issues: list[str] = []

    target: ast.ClassDef | None
    if expected.class_name is not None:
        target = next((c for c in classes if c.name == expected.class_name), None)
        if target is None:
            issues.append(f"expected class '{expected.class_name}' not found")
    else:
        target = classes[0] if classes else None
        if target is None and (
            expected.base_class is not None or expected.required_methods
        ):
            issues.append("no class definition found")

    if target is not None:
        if expected.base_class is not None and expected.base_class not in _base_names(
            target
        ):
            issues.append(
                f"class '{target.name}' does not inherit expected base "
                f"'{expected.base_class}'"
            )
        if expected.required_methods:
            defined = {
                item.name
                for item in target.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for method in expected.required_methods:
                if method not in defined:
                    issues.append(
                        f"class '{target.name}' missing required method '{method}'"
                    )

    return tuple(issues)


def validate_generated_code(
    source_text: str, expected: ModelExpectedStructure | None = None
) -> ModelGeneratedCodeValidation:
    """Validate a generated code artifact and return typed diagnostics.

    Pure: parses the given text, performs no I/O, and returns a deterministic
    result. A non-parseable artifact short-circuits with ``is_valid=False`` and
    the syntax error captured; the stub and structure checks run only on
    parseable source.
    """
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        return ModelGeneratedCodeValidation(
            parses=False,
            syntax_error=str(exc),
            stub_methods=(),
            structure_issues=(),
            is_valid=False,
        )

    comments = _comment_lines(source_text)
    stub_methods = _detect_stub_methods(tree, comments)
    structure_issues = _check_structure(tree, expected) if expected is not None else ()

    return ModelGeneratedCodeValidation(
        parses=True,
        syntax_error=None,
        stub_methods=stub_methods,
        structure_issues=structure_issues,
        is_valid=not stub_methods and not structure_issues,
    )


class HandlerGeneratedCodeValidator:
    """Validate a generated code artifact into a ModelGeneratedCodeValidation."""

    def handle(
        self, request: ModelGeneratedCodeValidatorRequest
    ) -> ModelGeneratedCodeValidation:
        return validate_generated_code(request.source_text, request.expected)


__all__ = ["HandlerGeneratedCodeValidator", "validate_generated_code"]
