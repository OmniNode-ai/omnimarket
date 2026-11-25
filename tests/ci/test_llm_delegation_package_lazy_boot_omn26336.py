# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26336: importing one delegation transport module does not wire every handler.

The composed boot check read a 2.07 s event-loop hold in the bootstrap import of
a handler module whose chain reaches this package through its transport module.
The package root imported every handler eagerly, so one transport import pulled
the router, the harness handlers and the credential probes in on the loop.
"""

from __future__ import annotations

import json
import subprocess
import sys

PACKAGE = "omnimarket.nodes.node_llm_delegation_call_effect"
HEAVY = (
    f"{PACKAGE}.handlers.handler_delegation_target",
    f"{PACKAGE}.handlers.handler_harness_delegation",
    f"{PACKAGE}.handlers.handler_agent_session",
    "omnimarket.nodes.node_model_router.handlers.handler_model_router",
)


def _loaded_after(statement: str) -> dict[str, bool]:
    code = (
        "import sys\n"
        f"{statement}\n"
        f"import json\n"
        f"print(json.dumps({{m: m in sys.modules for m in {HEAVY!r}}}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    loaded: dict[str, bool] = json.loads(out.stdout)
    return loaded


def test_transport_import_loads_no_handler_module() -> None:
    loaded = _loaded_after(f"import {PACKAGE}.handlers.transport")
    assert not any(loaded.values()), loaded


def test_public_names_still_resolve_from_the_package_root() -> None:
    code = (
        f"import {PACKAGE} as p\n"
        "missing = [n for n in p.__all__ if getattr(p, n, None) is None]\n"
        "print(missing)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
