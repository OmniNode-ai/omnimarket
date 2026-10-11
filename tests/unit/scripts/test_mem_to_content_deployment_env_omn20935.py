# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The memory-to-content benchmark script takes its tenant from the environment (OMN-20935)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "delegation_false_pass_growth"
    / "mem_to_content.py"
)


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "   "])
def test_the_script_refuses_without_a_tenant_id(value: str | None) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ONEX_TENANT_ID"}
    if value is not None:
        env["ONEX_TENANT_ID"] = value
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "ONEX_TENANT_ID is not configured" in result.stderr
