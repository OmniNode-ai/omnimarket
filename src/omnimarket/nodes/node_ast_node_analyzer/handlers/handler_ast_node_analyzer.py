"""Pure deterministic AST analysis of an ONEX node source file — no I/O, no side effects."""

from __future__ import annotations

import ast

from omnimarket.nodes.node_ast_node_analyzer.models.model_ast_node_analyzer_request import (
    ModelAstNodeAnalyzerRequest,
)
from omnimarket.nodes.node_ast_node_analyzer.models.model_node_analysis import (
    ModelNodeAnalysis,
)

# Import-name -> I/O operation class. A node importing any listed module is
# treated as performing that operation class.
_HTTP_MODULES = ("httpx", "requests", "aiohttp", "urllib")
_DATABASE_MODULES = ("asyncpg", "sqlalchemy", "psycopg2", "pymongo")
_MESSAGE_QUEUE_MODULES = ("aiokafka", "redis", "pika")

# Substring signals for file I/O detected directly in the source text.
_FILE_IO_SIGNALS = ("open(", "Path(")

# Returned when no specific I/O class is detected — the node is pure computation.
_DEFAULT_OPERATION = "computation"


def _collect_imports(tree: ast.AST) -> dict[str, list[str]]:
    """Map imported module name -> imported symbol names."""
    imports: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imports[node.module] = [a.name for a in node.names if a.name]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.name] = [alias.name]
    return imports


def _detect_io_operations(
    source_text: str, imports: dict[str, list[str]]
) -> tuple[str, ...]:
    """Infer I/O operation classes from imports and source substrings.

    Deterministic order: http_request, database_query, message_queue, file_io.
    When no operation class is detected the node is pure ``computation``.
    """
    operations: list[str] = []
    if any(module in imports for module in _HTTP_MODULES):
        operations.append("http_request")
    if any(module in imports for module in _DATABASE_MODULES):
        operations.append("database_query")
    if any(module in imports for module in _MESSAGE_QUEUE_MODULES):
        operations.append("message_queue")
    if any(signal in source_text for signal in _FILE_IO_SIGNALS):
        operations.append("file_io")
    if not operations:
        operations.append(_DEFAULT_OPERATION)
    return tuple(operations)


def _find_node_class(tree: ast.AST) -> ast.ClassDef:
    """Return the first class inheriting a ``Node*`` base, else raise ValueError."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id.startswith("Node"):
                    return node
    raise ValueError(
        "no ONEX node class (a class inheriting a `Node*` base) found in source"
    )


def analyze_node_source(source_text: str) -> ModelNodeAnalysis:
    """Parse a single ONEX node source file and return its structural analysis.

    Pure: parses the given text, performs no I/O, and returns a deterministic
    result. Raises ``ValueError`` when the text is not parseable Python or
    contains no ONEX node class.
    """
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise ValueError(f"source is not valid Python: {exc}") from exc

    node_class = _find_node_class(tree)
    imports = _collect_imports(tree)

    base_class = ""
    mixins: list[str] = []
    for base in node_class.bases:
        if isinstance(base, ast.Name):
            if base.id.startswith("Node"):
                base_class = base.id
            elif base.id.startswith("Mixin"):
                mixins.append(base.id)

    methods = tuple(
        item.name
        for item in node_class.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    )

    return ModelNodeAnalysis(
        class_name=node_class.name,
        base_class=base_class,
        mixins=tuple(mixins),
        methods=methods,
        docstring=ast.get_docstring(node_class),
        io_operations=_detect_io_operations(source_text, imports),
    )


class HandlerAstNodeAnalyzer:
    """Analyze an ONEX node source file into a structured ModelNodeAnalysis."""

    def handle(self, request: ModelAstNodeAnalyzerRequest) -> ModelNodeAnalysis:
        return analyze_node_source(request.source_text)


__all__ = ["HandlerAstNodeAnalyzer", "analyze_node_source"]
