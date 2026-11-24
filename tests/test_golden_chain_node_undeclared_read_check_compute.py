# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_undeclared_read_check_compute, driven only by what its contract declares.

The check is run in process by a CI job and a pre-commit hook, so the chain is the contract itself: its declared handler, input
model and output model are resolved by name and one request runs through the handler, once with a declared read and once with an
undeclared one (the error leg), which must be refused.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_undeclared_read_check_compute/contract.yaml"
)
_DIFF = (
    "diff --git a/src/omnimarket/x.py b/src/omnimarket/x.py\n--- a/src/omnimarket/x.py\n+++ b/src/omnimarket/x.py\n"
    '@@ -1,0 +1,1 @@\n+value = os.environ["ONEX_GOLDEN_NAME"]\n'
)


def _resolve(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


def _run(documents: dict[str, Any]) -> Any:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    handler = _resolve(contract["handler"]["module"], contract["handler"]["class"])()
    request_cls = _resolve(
        contract["input_model"]["module"], contract["input_model"]["name"]
    )
    result = handler.handle(request_cls(diff_text=_DIFF, overlay_documents=documents))
    assert isinstance(
        result,
        _resolve(contract["output_model"]["module"], contract["output_model"]["name"]),
    )
    return result


def test_the_golden_leg_admits_a_declared_read() -> None:
    result = _run(
        {"declared_settings": {"declared_env": ["host:* ONEX_GOLDEN_NAME=x"]}}
    )
    assert len(result.reads) == 1
    assert not result.refused


def test_the_error_leg_refuses_an_undeclared_read() -> None:
    result = _run({"declared_settings": {"declared_env": []}})
    assert result.refused
    assert [read.name for read in result.undeclared] == ["ONEX_GOLDEN_NAME"]
