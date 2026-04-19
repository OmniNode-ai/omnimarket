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


def _resolve_omni_home() -> str:
    """Resolve OMNI_HOME at call-time (not import-time) so tests stay hermetic.

    Order of preference:
      1. Explicit OMNI_HOME env var (operator override).
      2. Walk up from __file__ looking for a sibling directory that contains
         the expected repo clones (matches the canonical omni_home layout).
      3. Fall back to ~/omni_home.
    """
    env_override = os.environ.get("OMNI_HOME")
    if env_override and Path(env_override).is_dir():
        return env_override
    # Walk up from this file: .../omni_home/omnimarket/tests/test_skill_dispatch.py
    # or .../omni_worktrees/<ticket>/omnimarket/tests/test_skill_dispatch.py
    for parent in Path(__file__).resolve().parents:
        if (parent / "omnibase_core").is_dir() and (parent / "omnimarket").is_dir():
            return str(parent)
    return str(Path.home() / "omni_home")


def _make_hermetic_omni_home(tmpdir: Path) -> str:
    """Build a throwaway omni_home with empty stubs so nodes can scan without
    depending on the operator's real checkout layout.
    """
    for repo in ("omnibase_core", "omnimarket", "omnibase_infra", "omniclaude"):
        (tmpdir / repo / "src").mkdir(parents=True, exist_ok=True)
    return str(tmpdir)


def _run_node_subprocess(
    module: str,
    extra_args: list[str],
    *,
    omni_home: str | None = None,
) -> dict[str, Any]:
    """Run a node module as a subprocess and return parsed JSON stdout.

    OMNI_HOME is resolved at call time (not module import) so tests can
    override per-case with a hermetic tempdir.
    """
    resolved = omni_home or _resolve_omni_home()
    cmd = [sys.executable, "-m", module, "--dry-run", *extra_args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "OMNI_HOME": resolved},
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


@pytest.fixture
def hermetic_omni_home(tmp_path: Path) -> str:
    """Scratch omni_home with empty repo stubs for sweep-style node tests."""
    return _make_hermetic_omni_home(tmp_path)


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
def test_node_dry_run_exits_and_writes_json(
    module: str, extra_args: list[str], hermetic_omni_home: str
) -> None:
    """Each node must exit 0 or 1 and write valid JSON to stdout on --dry-run."""
    data = _run_node_subprocess(module, extra_args, omni_home=hermetic_omni_home)
    assert isinstance(data, dict), f"{module}: stdout is not a JSON object"
    assert "status" in data or "findings" in data, (
        f"{module}: JSON output missing both 'status' and 'findings' keys — "
        f"does not look like a node result model"
    )


# ---------------------------------------------------------------------------
# Parity: subprocess output matches handler.handle() directly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_coverage_sweep_parity(hermetic_omni_home: str) -> None:
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
            omni_home=hermetic_omni_home,
        )
        assert "status" in proc_data
        assert "repos_scanned" in proc_data
        assert "gaps" in proc_data
        assert "dry_run" in proc_data

        # Both outputs must be parseable as the same result model AND share
        # the same field surface + dry_run flag (CR parity assertion).
        proc_result = CoverageSweepResult.model_validate(proc_data)
        assert proc_result.dry_run == direct_result.dry_run
        assert proc_result.model_dump().keys() == direct_result.model_dump().keys()


@pytest.mark.unit
def test_runtime_sweep_parity(hermetic_omni_home: str) -> None:
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
        omni_home=hermetic_omni_home,
    )
    assert "findings" in proc_data
    assert "status" in proc_data
    assert "dry_run" in proc_data

    proc_result = RuntimeSweepResult.model_validate(proc_data)
    assert proc_result.dry_run == direct_result.dry_run
    assert proc_result.model_dump().keys() == direct_result.model_dump().keys()


@pytest.mark.unit
def test_aislop_sweep_parity(hermetic_omni_home: str) -> None:
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
            omni_home=hermetic_omni_home,
        )
        assert "findings" in proc_data
        assert "status" in proc_data

        proc_result = AislopSweepResult.model_validate(proc_data)
        assert proc_result.model_dump().keys() == direct_result.model_dump().keys()


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
