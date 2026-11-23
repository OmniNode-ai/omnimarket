# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23413: the setup CLI imports without fcntl, so Windows gets the WSL remedy.

#6594 imported fcntl at the top of omnimarket.handlers.model_catalog. The setup
CLI imports that module (through the endpoint catalog) before its preflight
runs, so on Windows ``onex setup`` died with ModuleNotFoundError instead of
printing the WSL remedy #6549 added, and dev's Tests (Split 2/20) failed.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "module",
    [
        "omnimarket.handlers.model_catalog",
        "omnimarket.nodes.node_onex_setup_effect.cli",
    ],
)
def test_module_imports_where_fcntl_does_not_exist(module: str) -> None:
    script = f"import sys\nsys.modules['fcntl'] = None\nimport {module}\n"
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"{module} needs fcntl at import:\n{result.stderr}"
