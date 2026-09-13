"""Extracted verbatim from omnimarket src/omnimarket/analysis/ast_import_scanner.py."""

import ast


def extract_imports(source: str) -> list[str]:
    """Return full dotted module names referenced in import statements.

    For ``import foo.bar``, returns ``"foo.bar"``.
    For ``from foo.bar import baz``, returns ``"foo.bar"``.
    The full dotted path is returned, NOT the top-level package only, so callers
    can perform exact path resolution instead of prefix matching.
    Invalid source returns an empty list rather than raising.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".")[0])
    return names
