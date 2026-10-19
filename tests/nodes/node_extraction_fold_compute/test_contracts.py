# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_extraction_fold_compute declares its topics, resolves its handler and has no envelope (OMN-21139, TE.2)."""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_extraction_fold_compute"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
COMMAND = "onex.cmd.omniintelligence.extraction-fold-requested.v1"
RESULT = "onex.cmd.omniintelligence.extraction-folded.v1"
ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "hashlib",
    "json",
    "re",
    "typing",
    "pydantic",
    "omnibase_core",
    "omnimarket",
}
FORBIDDEN_IMPORT_WORDS = ("ledger", "delegat", "llm", "openai", "anthropic", "httpx")


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_the_contract_declares_its_command_and_result_topics() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [RESULT]
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND
    assert contract["terminal_event"] == RESULT


def test_the_content_bearing_topics_are_on_the_restricted_family() -> None:
    """Payloads carry transcript or operator words: both topics are onex.cmd.omniintelligence.*, none onex.evt.*."""
    bus = _contract()["event_bus"]
    for topic in bus["subscribe_topics"] + bus["publish_topics"]:
        assert topic.startswith("onex.cmd.omniintelligence."), topic


def test_the_contract_handler_resolves_to_a_class_with_handle() -> None:
    handler = _contract()["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert callable(cls.handle)
    assert _contract()["descriptor"]["purity"] == "pure"


def test_the_node_is_registered_as_an_onex_node_entry_point() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entries[NODE] == f"omnimarket.nodes.{NODE}"


def test_the_node_package_imports_no_event_envelope() -> None:
    sources = list(NODE_DIR.rglob("*.py"))
    assert sources
    for path in sources:
        assert "ModelEventEnvelope" not in path.read_text(encoding="utf-8"), path


def test_the_node_imports_no_model_client_and_no_ledger_reader() -> None:
    handler = NODE_DIR / "handlers" / "handler_extraction_fold.py"
    imports = _imports(handler)
    assert imports, "positive control: the handler has imports"
    for name in imports:
        assert name.split(".")[0] in ALLOWED_IMPORT_ROOTS, name
        assert not any(word in name.lower() for word in FORBIDDEN_IMPORT_WORDS), name


def test_the_handler_hardcodes_no_topic_string() -> None:
    text = (NODE_DIR / "handlers" / "handler_extraction_fold.py").read_text(
        encoding="utf-8"
    )
    assert "onex.cmd." not in text
    assert "onex.evt." not in text
