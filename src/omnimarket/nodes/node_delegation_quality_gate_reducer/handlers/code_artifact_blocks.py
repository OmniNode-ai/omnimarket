# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fenced code blocks and the artifact test the quality gate applies to them."""

from __future__ import annotations

import ast
import json
import re

import yaml

# Closing fences occupy their own line; backticks inside code are body text.
_MARKDOWN_FENCE_RE = re.compile(
    r"```(?:[^\r\n]*)\r?\n(.*?)^[ ]{0,3}```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
_MARKDOWN_FENCE_WITH_LANG_RE = re.compile(
    r"```([A-Za-z0-9_-]*)[^\r\n]*\r?\n(.*?)^[ ]{0,3}```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)

_SEARCH_REPLACE_EDIT_RE = re.compile(
    r"(?ms)^FILE:\s*(?P<file>[^\r\n]+)\r?\n"
    r"<<<<<<< SEARCH\r?\n(?P<search>.*?)\r?\n"
    r"=======\r?\n(?P<replace>.*?)\r?\n"
    r">>>>>>> REPLACE(?:\r?\n|$)"
)
_REQUESTED_SYMBOL_RE = re.compile(
    r"(?ix)\b(?:function|class|method|symbol|constant|variable)\s+"
    r"(?:named\s+)?[`'\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)

# OMN-19734 (e18466cc, 1eaa0f6e): only declared Python, YAML, and JSON fences
# have parsers here. Other language tags and SEARCH/REPLACE edit fragments are
# unevaluated.
_PYTHON_FENCE_LANG_TAGS: frozenset[str] = frozenset(
    {"", "python", "py", "python3", "py3"}
)
_YAML_FENCE_LANG_TAGS: frozenset[str] = frozenset({"yaml", "yml"})
_JSON_FENCE_LANG_TAGS: frozenset[str] = frozenset({"json"})
_SEARCH_REPLACE_BLOCK_RE = re.compile(
    r"^<<<<<<< SEARCH[ \t]*\r?\n.*?^=======[ \t]*\r?\n.*?^>>>>>>> REPLACE[ \t]*\r?$",
    re.MULTILINE | re.DOTALL,
)


def _remove_search_replace_blocks(content: str) -> str:
    """Remove SEARCH/REPLACE edit fragments before parsing an answer."""
    return _SEARCH_REPLACE_BLOCK_RE.sub("", content)


def _strip_markdown_code_fence(content: str) -> str:
    """Return fenced code body when content is a single markdown code block."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return content
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return content


def _extract_fenced_code_blocks(content: str) -> list[str]:
    """Return all fenced code block bodies from mixed content."""
    return _MARKDOWN_FENCE_RE.findall(content)


def _extract_fenced_code_blocks_with_lang(content: str) -> list[tuple[str, str]]:
    """Return (lang_tag, body) pairs for all fenced code blocks in mixed content.

    ``lang_tag`` is the lowercased fence-info-string token (e.g. ``"yaml"`` for
    ` ```yaml`), or ``""`` when the fence carries no language tag.
    """
    return [
        (lang.lower(), body)
        for lang, body in _MARKDOWN_FENCE_WITH_LANG_RE.findall(content)
    ]


def _compiles_without_errors_is_evaluable(content: str) -> bool:
    """Whether the answer contains an artifact this check can parse."""
    content_without_edit_blocks = _remove_search_replace_blocks(content)
    tagged_blocks = _extract_fenced_code_blocks_with_lang(content_without_edit_blocks)
    if not tagged_blocks:
        return content_without_edit_blocks == content
    supported = _PYTHON_FENCE_LANG_TAGS | _YAML_FENCE_LANG_TAGS | _JSON_FENCE_LANG_TAGS
    if content_without_edit_blocks == content:
        return any(lang in supported for lang, _ in tagged_blocks)
    return any(lang in supported and body.strip() for lang, body in tagged_blocks)


def _remove_fenced_code_blocks(content: str) -> str:
    """Return response text outside fenced code blocks."""
    return _MARKDOWN_FENCE_RE.sub("", content).strip()


def _is_search_replace_artifact(content: str) -> bool:
    """Return whether content is one or more complete SEARCH/REPLACE edits."""
    matches = tuple(_SEARCH_REPLACE_EDIT_RE.finditer(content.strip()))
    if not matches:
        return False
    cursor = 0
    for match in matches:
        if content.strip()[cursor : match.start()].strip():
            return False
        if not match.group("file").strip():
            return False
        if not (match.group("search").strip() or match.group("replace").strip()):
            return False
        cursor = match.end()
    return not content.strip()[cursor:].strip()


def _is_json_edit_artifact(content: str) -> bool:
    """Return whether content is a non-empty structured edit envelope."""
    try:
        loaded = json.loads(content)
    except json.JSONDecodeError:
        return False
    if not isinstance(loaded, dict):
        return False
    edits = loaded.get("edits")
    if not isinstance(edits, list) or not edits:
        return False
    for edit in edits:
        if not isinstance(edit, dict):
            return False
        file_name = edit.get("file")
        search = edit.get("search")
        replacement = edit.get("replace")
        if not isinstance(file_name, str) or not file_name.strip():
            return False
        if not isinstance(search, str) or not isinstance(replacement, str):
            return False
        if not (search.strip() or replacement.strip()):
            return False
    return True


def _defined_python_symbols(tree: ast.AST) -> frozenset[str]:
    """Return symbols explicitly defined by a parsed Python artifact."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.partition(".")[0])
    return frozenset(names)


def _requested_symbols(task_prompt: str | None) -> frozenset[str]:
    """Extract symbols a prompt explicitly asks to define or change."""
    if task_prompt is None:
        return frozenset()
    return frozenset(
        match.group("name") for match in _REQUESTED_SYMBOL_RE.finditer(task_prompt)
    )


def _python_tree_is_code_artifact(
    tree: ast.Module,
    *,
    task_prompt: str | None,
) -> bool:
    """Reject bare literals/names while accepting executable code structure."""
    requested = _requested_symbols(task_prompt)
    if requested & _defined_python_symbols(tree):
        return True

    for statement in tree.body:
        if isinstance(statement, ast.Expr):
            if not isinstance(statement.value, ast.Name | ast.Constant):
                return True
            continue
        if not isinstance(statement, ast.Pass):
            return True
    return False


def _block_is_code_artifact(
    body: str,
    *,
    language: str,
    task_prompt: str | None,
) -> bool:
    """Evaluate a fenced block according to its declared artifact language."""
    if language in _YAML_FENCE_LANG_TAGS:
        try:
            loaded = yaml.safe_load(body)
        except yaml.YAMLError:
            return False
        return isinstance(loaded, dict | list) and bool(loaded)
    if language in _JSON_FENCE_LANG_TAGS:
        try:
            loaded = json.loads(body)
        except json.JSONDecodeError:
            return False
        return isinstance(loaded, dict | list) and bool(loaded)
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return False
    return _python_tree_is_code_artifact(tree, task_prompt=task_prompt)
