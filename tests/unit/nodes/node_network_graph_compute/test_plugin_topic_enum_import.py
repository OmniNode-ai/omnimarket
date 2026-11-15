# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23141 AC1: no market source imports the claude plugin's topic enum.

The cut removes the one optional import (``omniclaude.hooks.topics``) that tied market to the plugin
package; topic names come from node contracts. The scan reads every market source file as a syntax
tree, so a comment or a docstring that names the module is not an import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[4] / "src" / "omnimarket"
_PLUGIN_TOPIC_MODULE = "omniclaude.hooks.topics"


def _imports_plugin_topic_enum(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == _PLUGIN_TOPIC_MODULE or node.module.startswith(
                _PLUGIN_TOPIC_MODULE + "."
            ):
                return True
            if node.module == "omniclaude.hooks" and any(
                alias.name == "topics" for alias in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name == _PLUGIN_TOPIC_MODULE
            or alias.name.startswith(_PLUGIN_TOPIC_MODULE + ".")
            for alias in node.names
        ):
            return True
    return False


def test_plugin_topic_enum_import_scan_finds_each_import_form() -> None:
    """Positive control: the scan reads RED on every spelling of the import, GREEN on prose."""
    assert _imports_plugin_topic_enum("from omniclaude.hooks.topics import TopicBase\n")
    assert _imports_plugin_topic_enum("from omniclaude.hooks import topics\n")
    assert _imports_plugin_topic_enum("import omniclaude.hooks.topics\n")
    assert _imports_plugin_topic_enum(
        "def f():\n    try:\n        from omniclaude.hooks.topics import TopicBase\n"
        "    except ImportError:\n        pass\n"
    )
    assert not _imports_plugin_topic_enum(
        '"""mentions omniclaude.hooks.topics.TopicBase"""\n# omniclaude.hooks.topics\n'
    )


def test_plugin_topic_enum_import_market_source_holds_none() -> None:
    files = sorted(_SRC.rglob("*.py"))
    assert files, "the scan found no market source, so a zero would prove nothing"
    offenders = [
        str(path.relative_to(_SRC))
        for path in files
        if _imports_plugin_topic_enum(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
