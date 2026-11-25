# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation handler wiring at boot must not import the workflow handler module.

OMN-26336: ``wire_delegation_handlers`` runs inside the kernel bootstrap coroutine.
Importing ``handler_delegation_workflow`` there held the event loop 1.149 s in the
composed-runtime boot check (job 137964427003). The check runs in a fresh interpreter
because the rest of the suite already holds the module in ``sys.modules``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

_HANDLER_MODULE = (
    "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow"
)

_PROBE = textwrap.dedent(
    f"""
    import asyncio
    import json
    import sys
    from unittest.mock import AsyncMock, MagicMock

    from omnimarket.nodes.node_delegation_orchestrator import wiring

    container = MagicMock()
    container.service_registry.register_instance = AsyncMock()
    result = asyncio.run(wiring.wire_delegation_handlers(container))
    loaded_after_boot = "{_HANDLER_MODULE}" in sys.modules

    handler = wiring.get_shared_delegation_workflow_handler()
    loaded_after_first_use = "{_HANDLER_MODULE}" in sys.modules

    print(json.dumps({{
        "status": result["status"],
        "loaded_after_boot": loaded_after_boot,
        "loaded_after_first_use": loaded_after_first_use,
        "shared": handler is wiring.get_shared_delegation_workflow_handler(),
    }}))
    """
)


@pytest.mark.unit
def test_boot_wiring_does_not_import_the_workflow_handler_module() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    observed = json.loads(completed.stdout.strip().splitlines()[-1])

    assert observed["status"] == "success"
    assert observed["loaded_after_boot"] is False
    assert observed["loaded_after_first_use"] is True
    assert observed["shared"] is True
