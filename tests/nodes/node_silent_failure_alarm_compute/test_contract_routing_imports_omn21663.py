# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every handler_routing path of the silent-failure compute contract imports (OMN-21663).

The recorded case: the ``decide_install_failure`` entry named a models module the node did not have, so the
operation could not be loaded from its own contract. The check reads the contract and imports every handler class and
every input and output model it names.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_silent_failure_alarm_compute as node

pytestmark = pytest.mark.unit

CONTRACT = Path(node.__file__).parent / "contract.yaml"


def _entries() -> list[dict[str, Any]]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    entries = document["handler_routing"]["handlers"]
    assert isinstance(entries, list)
    assert entries
    return entries


def _import_path(dotted: str) -> object:
    module, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize("entry", _entries(), ids=lambda e: str(e["operation"]))
def test_every_handler_routing_path_imports(entry: dict[str, Any]) -> None:
    handler = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    assert callable(getattr(handler, "handle", None))
    assert isinstance(_import_path(entry["input_model"]), type)
    assert isinstance(_import_path(entry["output_model"]), type)


def test_the_install_failure_operation_is_routed() -> None:
    operations = {entry["operation"] for entry in _entries()}
    assert "decide_install_failure" in operations
