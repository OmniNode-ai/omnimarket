# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch binding annotations and construction work in a fresh interpreter."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.unit
@pytest.mark.parametrize("first", ["handler", "container"])
def test_dispatch_binding_import_order(first: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import sys
from typing import get_type_hints

root = "omnimarket.nodes.node_skill_dispatch_engine_orchestrator"
paths = {
    "handler": root + ".handlers.handler_dispatch_router",
    "container": root + ".container.container_skill_dispatch_engine_orchestrator",
}
importlib.import_module(paths[sys.argv[1]])
handler = importlib.import_module(paths["handler"])
binding = importlib.import_module(paths["container"])
build = binding.create_handler_dispatch_engine_router
assert get_type_hints(build)["return"] is handler.HandlerDispatchEngineRouter
router = build()
assert type(router) is handler.HandlerDispatchEngineRouter
assert type(router._rsd) is binding.HandlerRsdFill
assert get_type_hints(handler.HandlerDispatchEngineRouter.__init__)["rsd_handler"] == binding.HandlerRsdFill | None
""",
            first,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
