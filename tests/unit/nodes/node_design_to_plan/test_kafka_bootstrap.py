# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests: node_design_to_plan Kafka bootstrap does not fall back to localhost.

Regression guard for OMN-11757: node_design_to_plan connected to localhost:19092
when KAFKA_BOOTSTRAP_SERVERS was unset instead of failing fast.

Root cause: old omnibase_core (<=0.40.0) had
    bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
in cli_run_node.py. Current omnibase_core uses os.environ["KAFKA_BOOTSTRAP_SERVERS"]
(KeyError on missing env, no silent localhost fallback).

These tests verify:
1. The contract declares KAFKA_BOOTSTRAP_SERVERS as a required env dependency.
2. The omnibase_core version installed in this env does not use a localhost default.
3. No source file in node_design_to_plan hardcodes "localhost:19092".
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
import yaml

NODE_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_design_to_plan"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"


# ---------------------------------------------------------------------------
# Contract env_dependencies
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContractEnvDependencies:
    """node_design_to_plan contract must declare KAFKA_BOOTSTRAP_SERVERS."""

    def test_contract_declares_kafka_bootstrap_servers(self) -> None:
        """Contract must have env_dependencies.KAFKA_BOOTSTRAP_SERVERS."""
        assert CONTRACT_PATH.exists(), f"contract.yaml not found at {CONTRACT_PATH}"
        contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
        env_deps = contract.get("env_dependencies", {})
        assert "KAFKA_BOOTSTRAP_SERVERS" in env_deps, (
            "contract.yaml must declare KAFKA_BOOTSTRAP_SERVERS under env_dependencies. "
            "This ensures operators know this node requires a Kafka bootstrap address "
            "and prevents silent localhost fallback (OMN-11757)."
        )

    def test_kafka_bootstrap_servers_is_required(self) -> None:
        """KAFKA_BOOTSTRAP_SERVERS must be marked required: true in contract."""
        contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
        env_deps = contract.get("env_dependencies", {})
        kafka_dep = env_deps.get("KAFKA_BOOTSTRAP_SERVERS", {})
        assert kafka_dep.get("required") is True, (
            "KAFKA_BOOTSTRAP_SERVERS must be required: true in env_dependencies "
            "(OMN-11757: fail fast instead of connecting to localhost:19092)"
        )


# ---------------------------------------------------------------------------
# No localhost hardcode in node source files
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoLocalhostHardcode:
    """node_design_to_plan source files must not hardcode localhost:19092."""

    def _iter_py_files(self) -> list[Path]:
        return list(NODE_DIR.rglob("*.py"))

    def test_no_localhost_19092_in_node_source(self) -> None:
        """No Python source in node_design_to_plan may hardcode localhost:19092."""
        violations: list[str] = []
        for path in self._iter_py_files():
            content = path.read_text(encoding="utf-8")
            if "localhost:19092" in content or "127.0.0.1:19092" in content:
                violations.append(str(path))
        assert violations == [], (
            "Hardcoded localhost:19092 found in node_design_to_plan source: "
            f"{violations}. Use os.environ['KAFKA_BOOTSTRAP_SERVERS'] instead "
            "(CLAUDE.md rule #8: fail-fast on missing env)."
        )

    def test_no_localhost_bootstrap_default_in_node_source(self) -> None:
        """No code in node_design_to_plan may use environ.get(..., 'localhost:19092')."""
        violations: list[tuple[str, int]] = []
        for path in self._iter_py_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # Detect: os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:...")
                if not isinstance(node, ast.Call):
                    continue
                if len(node.args) < 2:
                    continue
                default_arg = node.args[1]
                if not isinstance(default_arg, ast.Constant):
                    continue
                val = default_arg.value
                if isinstance(val, str) and (
                    "localhost:" in val or "127.0.0.1:" in val
                ):
                    violations.append((str(path), node.lineno))
        assert violations == [], (
            "os.environ.get() with localhost default found in node_design_to_plan: "
            f"{violations}. Use os.environ['KAFKA_BOOTSTRAP_SERVERS'] to fail fast "
            "(OMN-11757)."
        )


# ---------------------------------------------------------------------------
# omnibase_core run-node bootstrap resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunNodeBootstrapResolution:
    """Verify the installed omnibase_core cli_run_node.py does not use localhost default."""

    def _find_cli_run_node(self) -> Path | None:
        """Find cli_run_node.py in the installed omnibase_core package."""
        try:
            import omnibase_core

            pkg_root = Path(omnibase_core.__file__).parent
            candidate = pkg_root / "cli" / "cli_run_node.py"
            return candidate if candidate.exists() else None
        except ImportError:
            return None

    def test_cli_run_node_does_not_use_localhost_default(self) -> None:
        """Installed omnibase_core cli_run_node.py must not default to localhost:19092.

        If this test fails, the installed omnibase_core version is too old.
        Update the venv: uv sync --all-extras (or rebuild the plugin venv).
        """
        path = self._find_cli_run_node()
        if path is None:
            pytest.skip("omnibase_core not installed or cli_run_node.py not found")

        content = path.read_text(encoding="utf-8")
        # The old pattern: os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
        assert 'localhost:19092"' not in content, (
            f"Installed omnibase_core at {path} uses localhost:19092 as a default "
            "bootstrap address. This is the root cause of OMN-11757. "
            "The fix requires updating to omnibase_core >=0.42.0 which uses "
            "os.environ['KAFKA_BOOTSTRAP_SERVERS'] (no fallback). "
            "Rebuild the affected venv: uv sync --all-extras"
        )

    def test_cli_run_node_uses_key_access_not_get(self) -> None:
        """Installed cli_run_node.py should use os.environ[...] not .get(..., default)."""
        path = self._find_cli_run_node()
        if path is None:
            pytest.skip("omnibase_core not installed or cli_run_node.py not found")

        content = path.read_text(encoding="utf-8")
        assert "KAFKA_BOOTSTRAP_SERVERS" in content, (
            "cli_run_node.py must reference KAFKA_BOOTSTRAP_SERVERS"
        )
        # Should use key access (raises KeyError on missing) not .get with localhost default
        has_key_access = 'os.environ["KAFKA_BOOTSTRAP_SERVERS"]' in content
        has_safe_get = (
            'os.environ.get("KAFKA_BOOTSTRAP_SERVERS")' in content
            and "localhost" not in content
        )
        assert has_key_access or has_safe_get, (
            f"cli_run_node.py at {path} does not use fail-fast KAFKA_BOOTSTRAP_SERVERS "
            "access. Expected os.environ['KAFKA_BOOTSTRAP_SERVERS'] (OMN-11757)."
        )

    def test_kafka_bootstrap_servers_present_or_skip(self) -> None:
        """When KAFKA_BOOTSTRAP_SERVERS is set, it must not be localhost:19092.

        This is a runtime environment check — skipped in CI where Kafka is absent.
        """
        bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
        if not bootstrap:
            pytest.skip(
                "KAFKA_BOOTSTRAP_SERVERS not set — skipping runtime address check"
            )

        assert not bootstrap.startswith("localhost:"), (
            f"KAFKA_BOOTSTRAP_SERVERS='{bootstrap}' points to localhost. "
            "This will fail when dispatching node_design_to_plan from a Mac "
            "to a remote Kafka broker. Set KAFKA_BOOTSTRAP_SERVERS to the remote "
            "Redpanda address (dev lane port 19092, stability-test lane port 39092)."
        )
        assert not bootstrap.startswith("127.0.0.1:"), (
            f"KAFKA_BOOTSTRAP_SERVERS='{bootstrap}' points to loopback 127.0.0.1. "
            "Set it to the remote Redpanda address (OMN-11757)."
        )
