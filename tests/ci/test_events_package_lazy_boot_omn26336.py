# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26336: importing one events submodule does not import every event module.

The composed boot check reads any call that holds the event loop over 1 s. The
bootstrap imports every handler module on the loop; a handler that reaches
``omnimarket.events`` through a protocol module imported the whole package root
(generation, runtime deployment, evidence pipeline wire models) for one model.
"""

from __future__ import annotations

import json
import subprocess
import sys

HEAVY = (
    "omnimarket.events.generation",
    "omnimarket.events.runtime_deployment",
    "omnimarket.events.emit_client",
    "omnimarket.events.llm_delegation_call",
)


def _run(code: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_one_events_submodule_import_loads_no_other_event_module() -> None:
    code = (
        "import sys, json\n"
        "import omnimarket.events.dod_verify_retry\n"
        f"print(json.dumps({{m: m in sys.modules for m in {HEAVY!r}}}))\n"
    )
    loaded = json.loads(_run(code))
    assert not any(loaded.values()), loaded


def test_every_public_name_still_resolves_from_the_package_root() -> None:
    code = (
        "import omnimarket.events as e\n"
        "print([n for n in e.__all__ if getattr(e, n, None) is None])\n"
    )
    assert _run(code) == "[]"
