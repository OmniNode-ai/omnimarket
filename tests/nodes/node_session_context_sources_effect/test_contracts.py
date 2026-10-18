# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_session_context_sources_effect declares its topics and resolves its handler (OMN-21108, TA.6)."""

from __future__ import annotations

import importlib
import inspect
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_session_context_sources_effect"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
COMMAND = "onex.cmd.omnimarket.session-context-sources-read-requested.v1"
EVENT = "onex.evt.omnimarket.session-context-sources-read-completed.v1"


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def test_the_contract_declares_its_command_and_terminal_event() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [EVENT]
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND
    assert contract["terminal_event"] == EVENT


def test_the_handler_has_the_context_source_signature() -> None:
    handler = _contract()["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    hints = inspect.get_annotations(cls.handle, eval_str=True)
    assert hints == {
        "request": ModelContextSourceRequest,
        "return": ModelContextSourceResult,
    }
    # The ProtocolContextSource flag: handle is a synchronous definition-B call.
    assert cls.synchronous_execution is True
    assert handler["input_model"].endswith("ModelContextSourceRequest")
    assert handler["output_model"].endswith("ModelContextSourceResult")


def test_the_node_is_registered_as_an_onex_node_entry_point() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entries[NODE] == f"omnimarket.nodes.{NODE}"


def test_the_node_package_imports_no_event_envelope_and_no_other_node() -> None:
    sources = list(NODE_DIR.rglob("*.py"))
    assert sources
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "ModelEventEnvelope" not in text, path
        for line in text.splitlines():
            if line.startswith(("from omnimarket.nodes.", "import omnimarket.nodes.")):
                assert f"omnimarket.nodes.{NODE}" in line, (path, line)


def test_the_handler_hardcodes_no_topic_string() -> None:
    for path in (NODE_DIR / "handlers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "onex.cmd." not in text, path
        assert "onex.evt." not in text, path
