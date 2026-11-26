# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each module on the backend-columns import path loads first in a fresh process (OMN-25861).

PostgresSyncProjectionAdapter mixes in the usage node's UsagePgStore, so the shared
adapter module imports the node's stores module. Failure mode: stores.py importing,
at module load, anything that leads back to the adapter module makes whichever of
the two a process imports first fail with a circular ImportError. A pytest session
imports them in one order, so each is imported first in its own interpreter here.
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_FIRST_IMPORTS = (
    "omnimarket.nodes.node_projection_usage_by_model_day.stores",
    "omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_store",
    "omnimarket.projection.postgres_sync_database",
    "omnimarket.projection.protocol_database",
    "omnimarket.projection.sqlite_database",
)


@pytest.mark.parametrize("module", _FIRST_IMPORTS)
def test_the_module_imports_first_in_a_fresh_process(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
