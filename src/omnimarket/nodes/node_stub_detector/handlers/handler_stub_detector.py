"""Pure deterministic detection of unimplemented method stubs — no I/O, no side effects.

The detector flags each ``async`` method of a class whose body carries a stub
marker. Comment markers are matched against real comment tokens (via ``tokenize``)
and the not-implemented marker against the AST (a real ``raise`` node), so a
marker that appears only inside a string literal is never a false positive.
"""

from __future__ import annotations

import ast
import io
import tokenize

from omnimarket.nodes.node_stub_detector.models.model_stub import ModelStub
from omnimarket.nodes.node_stub_detector.models.model_stub_detection_result import (
    ModelStubDetectionResult,
)
from omnimarket.nodes.node_stub_detector.models.model_stub_detector_request import (
    ModelStubDetectorRequest,
)

# Ordered comment markers: (marker_label, comment_needle). Precedence follows
# this order. The marker text lives only in these string constants — never in a
# bare code comment — so the repo's untracked-marker gate does not flag it.
_COMMENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("# IMPLEMENTATION REQUIRED", "IMPLEMENTATION REQUIRED"),
    ("# TODO:", "TODO:"),
    ("pass  # Stub", "Stub"),
)

# Lowest-precedence marker: a structural ``raise NotImplementedError`` statement.
_NOT_IMPLEMENTED_MARKER = "raise NotImplementedError"


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


def _raises_not_implemented(method: ast.AST) -> bool:
    """True when the method body contains a ``raise NotImplementedError`` node."""
    for node in ast.walk(method):
        if isinstance(node, ast.Raise):
            exc = node.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
            if isinstance(exc, ast.Attribute) and exc.attr == "NotImplementedError":
                return True
    return False


def _method_marker(
    method: ast.AsyncFunctionDef,
    comments: dict[int, list[str]],
) -> str | None:
    """Return the highest-precedence incomplete-method marker, else None."""
    start = method.lineno
    end = method.end_lineno or method.lineno
    in_range = "\n".join(
        text
        for lineno, texts in comments.items()
        if start <= lineno <= end
        for text in texts
    )
    for marker_label, needle in _COMMENT_MARKERS:
        if needle in in_range:
            return marker_label
    if _raises_not_implemented(method):
        return _NOT_IMPLEMENTED_MARKER
    return None


def _signature(method: ast.AsyncFunctionDef, source_text: str) -> str:
    """First source line of the method definition (the signature line)."""
    segment = ast.get_source_segment(source_text, method)
    if segment:
        return segment.split("\n", 1)[0]
    return f"async def {method.name}(...)"


def detect_stubs(source_text: str) -> tuple[ModelStub, ...]:
    """Detect incomplete async methods in an ONEX node source file.

    Pure: parses the given text, performs no I/O, and returns a deterministic
    tuple. Unparseable source yields an empty tuple.
    """
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return ()

    comments = _comment_lines(source_text)

    stubs: list[ModelStub] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef):
                    marker = _method_marker(item, comments)
                    if marker is not None:
                        stubs.append(
                            ModelStub(
                                method_name=item.name,
                                signature=_signature(item, source_text),
                                marker=marker,
                            )
                        )
    return tuple(stubs)


class HandlerStubDetector:
    """Detect unimplemented async-method stubs in an ONEX node source file."""

    def handle(self, request: ModelStubDetectorRequest) -> ModelStubDetectionResult:
        return ModelStubDetectionResult(stubs=detect_stubs(request.source_text))


__all__ = ["HandlerStubDetector", "detect_stubs"]
