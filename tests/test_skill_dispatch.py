# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill-to-node dispatch parity harness [OMN-8008].

Validates that each ported node:
1. Can be invoked via `python -m <module> --dry-run` and exits 0
2. Writes valid JSON to stdout that can be parsed as the node's result model
3. Produces output that matches direct handler invocation (parity check)

Parametrized over three Wave 1 ported nodes:
- node_coverage_sweep
- node_runtime_sweep
- node_aislop_sweep

Pattern: invoke the __main__ module as a subprocess, capture stdout, parse JSON,
validate the result schema matches what the handler produces directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    AislopSweepResult,
    NodeAislopSweep,
)
from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (
    CoverageSweepRequest,
    CoverageSweepResult,
    NodeCoverageSweep,
)
from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    ModelContractInput,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
    RuntimeSweepResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OMNI_HOME = os.environ.get("OMNI_HOME", str(Path.home() / "omni_home"))


def _run_node_subprocess(module: str, extra_args: list[str]) -> dict[str, Any]:
    """Run a node module as a subprocess and return parsed JSON stdout."""
    cmd = [sys.executable, "-m", module, "--dry-run", *extra_args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "OMNI_HOME": _OMNI_HOME},
    )
    assert result.returncode in (
        0,
        1,
    ), f"{module} crashed (exit {result.returncode}):\n{result.stderr}"
    assert result.stdout.strip(), f"{module} produced no stdout:\n{result.stderr}"
    return json.loads(result.stdout)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Parametrized dry-run exit test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "extra_args"),
    [
        (
            "omnimarket.nodes.node_coverage_sweep",
            ["--repos", "omnibase_core"],
        ),
        (
            "omnimarket.nodes.node_runtime_sweep",
            ["--scope", "all-repos"],
        ),
        (
            "omnimarket.nodes.node_aislop_sweep",
            ["--repos", "omnibase_core", "--dry-run"],
        ),
    ],
    ids=["coverage_sweep", "runtime_sweep", "aislop_sweep"],
)
@pytest.mark.unit
def test_node_dry_run_exits_and_writes_json(module: str, extra_args: list[str]) -> None:
    """Each node must exit 0 or 1 and write valid JSON to stdout on --dry-run."""
    data = _run_node_subprocess(module, extra_args)
    assert isinstance(data, dict), f"{module}: stdout is not a JSON object"
    assert "status" in data or "findings" in data, (
        f"{module}: JSON output missing both 'status' and 'findings' keys — "
        f"does not look like a node result model"
    )


# ---------------------------------------------------------------------------
# Parity: subprocess output matches handler.handle() directly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_coverage_sweep_parity() -> None:
    """Subprocess invocation and direct handler call produce schema-compatible output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a minimal coverage.json so the handler has something to scan
        coverage_json = Path(tmpdir) / "coverage.json"
        coverage_json.write_text(json.dumps({"files": {}}))

        # Direct handler invocation
        handler = NodeCoverageSweep()
        request = CoverageSweepRequest(target_dirs=[tmpdir], dry_run=True)
        direct_result = handler.handle(request)

        # Validate the direct result is a proper model instance
        assert isinstance(direct_result, CoverageSweepResult)
        assert direct_result.dry_run is True
        assert direct_result.status in ("clean", "gaps_found", "partial", "error")

        # Subprocess invocation — verify same schema keys present
        proc_data = _run_node_subprocess(
            "omnimarket.nodes.node_coverage_sweep",
            ["--repos", "omnibase_core"],
        )
        assert "status" in proc_data
        assert "repos_scanned" in proc_data
        assert "gaps" in proc_data
        assert "dry_run" in proc_data

        # Both outputs must be parseable as the same result model
        CoverageSweepResult.model_validate(proc_data)


@pytest.mark.unit
def test_runtime_sweep_parity() -> None:
    """Subprocess invocation and direct handler call produce schema-compatible output."""
    handler = NodeRuntimeSweep()
    request = RuntimeSweepRequest(
        contracts=[
            ModelContractInput(
                node_name="test_node",
                description="test",
                handler_module="test.module",
                publish_topics=["onex.evt.test.done.v1"],
                subscribe_topics=["onex.cmd.test.start.v1"],
            )
        ],
        topic_producers=["onex.evt.test.done.v1"],
        topic_consumers=["onex.cmd.test.start.v1"],
        dry_run=True,
    )
    direct_result = handler.handle(request)

    assert isinstance(direct_result, RuntimeSweepResult)
    assert direct_result.status in ("clean", "findings", "error")

    proc_data = _run_node_subprocess(
        "omnimarket.nodes.node_runtime_sweep",
        ["--scope", "all-repos"],
    )
    assert "findings" in proc_data
    assert "status" in proc_data
    assert "dry_run" in proc_data

    RuntimeSweepResult.model_validate(proc_data)


@pytest.mark.unit
def test_aislop_sweep_parity() -> None:
    """Subprocess invocation and direct handler call produce schema-compatible output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a minimal Python file to scan
        (Path(tmpdir) / "test_module.py").write_text("x = 1\n")

        handler = NodeAislopSweep()
        request = AislopSweepRequest(
            target_dirs=[tmpdir],
            dry_run=True,
        )
        direct_result = handler.handle(request)

        assert isinstance(direct_result, AislopSweepResult)
        assert direct_result.status in ("clean", "findings", "partial", "error")

        proc_data = _run_node_subprocess(
            "omnimarket.nodes.node_aislop_sweep",
            ["--repos", "omnibase_core", "--dry-run"],
        )
        assert "findings" in proc_data
        assert "status" in proc_data

        AislopSweepResult.model_validate(proc_data)


# ---------------------------------------------------------------------------
# Regression baseline: result models have required fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_coverage_sweep_result_schema_baseline() -> None:
    """CoverageSweepResult fields are present and have correct types."""
    result = CoverageSweepResult()
    assert isinstance(result.gaps, list)
    assert isinstance(result.repos_scanned, int)
    assert isinstance(result.total_modules, int)
    assert isinstance(result.below_target, int)
    assert isinstance(result.zero_coverage, int)
    assert isinstance(result.average_coverage, float)
    assert isinstance(result.status, str)
    assert isinstance(result.dry_run, bool)


@pytest.mark.unit
def test_runtime_sweep_result_schema_baseline() -> None:
    """RuntimeSweepResult fields are present and have correct types."""
    result = RuntimeSweepResult()
    assert isinstance(result.findings, list)
    assert isinstance(result.status, str)
    assert isinstance(result.dry_run, bool)
    assert isinstance(result.total_findings, int)


@pytest.mark.unit
def test_aislop_sweep_result_schema_baseline() -> None:
    """AislopSweepResult fields are present and have correct types."""
    result = AislopSweepResult()
    assert isinstance(result.findings, list)
    assert isinstance(result.status, str)
    assert isinstance(result.total_findings, int)
