# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for node_architectural_invariant_loop (OMN-11221)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_architectural_invariant_loop import (
    ArchInvariantLoopRequest,
    NodeArchitecturalInvariantLoop,
)
from omnimarket.nodes.node_architectural_invariant_loop.handlers.handler_architectural_invariant_loop import (
    _check_contract_driven_routing,
    _check_event_bus_di,
    _check_no_hardcoded_paths,
    _check_no_silent_fallback,
    _load_invariant_contracts,
)


def _make_handler() -> NodeArchitecturalInvariantLoop:
    return NodeArchitecturalInvariantLoop(event_bus=MagicMock())


@pytest.mark.unit
class TestLoadInvariantContracts:
    def test_loads_all_seed_contracts(self) -> None:
        contracts = _load_invariant_contracts(None)
        codes = {c["principle_code"] for c in contracts}
        assert codes == {"ARCH-001", "ARCH-002", "ARCH-003", "ARCH-004", "ARCH-005"}

    def test_filters_by_invariant_ids(self) -> None:
        contracts = _load_invariant_contracts(["ARCH-001", "ARCH-003"])
        codes = {c["principle_code"] for c in contracts}
        assert codes == {"ARCH-001", "ARCH-003"}

    def test_empty_filter_returns_none(self) -> None:
        contracts = _load_invariant_contracts([])
        assert contracts == []


@pytest.mark.unit
class TestCheckerNoSilentFallback:
    def _write_file(self, tmp: Path, content: str) -> Path:
        f = tmp / "test_module.py"
        f.write_text(content)
        return f

    def test_detects_silent_fallback(self, tmp_path: Path) -> None:
        py_file = self._write_file(
            tmp_path,
            'x = os.environ.get("OMNI_HOME", "/default")\n',
        )
        violations = _check_no_silent_fallback("myrepo", py_file, tmp_path)
        assert len(violations) == 1
        assert violations[0].principle_code == "ARCH-002"

    def test_skips_fallback_ok_annotation(self, tmp_path: Path) -> None:
        py_file = self._write_file(
            tmp_path,
            'x = os.environ.get("KEY", "val")  # fallback-ok: intentional\n',
        )
        violations = _check_no_silent_fallback("myrepo", py_file, tmp_path)
        assert violations == []

    def test_clean_file_no_violations(self, tmp_path: Path) -> None:
        py_file = self._write_file(tmp_path, 'x = os.environ["OMNI_HOME"]\n')
        violations = _check_no_silent_fallback("myrepo", py_file, tmp_path)
        assert violations == []


@pytest.mark.unit
class TestCheckerContractDrivenRouting:
    def test_detects_hardcoded_topic(self, tmp_path: Path) -> None:
        py_file = tmp_path / "handler.py"
        py_file.write_text('topic = "onex.cmd.omnimarket.foo.v1"\n')
        violations = _check_contract_driven_routing("myrepo", py_file, tmp_path)
        assert len(violations) == 1
        assert violations[0].principle_code == "ARCH-003"

    def test_skips_contract_topics_file(self, tmp_path: Path) -> None:
        py_file = tmp_path / "contract_topics.py"
        py_file.write_text('TOPIC = "onex.cmd.omnimarket.foo.v1"\n')
        violations = _check_contract_driven_routing("myrepo", py_file, tmp_path)
        assert violations == []


@pytest.mark.unit
class TestCheckerEventBusDI:
    def test_detects_none_assignment(self, tmp_path: Path) -> None:
        py_file = tmp_path / "handler.py"
        py_file.write_text("self._event_bus = None\n")
        violations = _check_event_bus_di("myrepo", py_file, tmp_path)
        assert len(violations) == 1
        assert violations[0].principle_code == "ARCH-004"

    def test_clean_di_assignment(self, tmp_path: Path) -> None:
        py_file = tmp_path / "handler.py"
        py_file.write_text("self._event_bus = event_bus\n")
        violations = _check_event_bus_di("myrepo", py_file, tmp_path)
        assert violations == []


@pytest.mark.unit
class TestCheckerNoHardcodedPaths:
    def test_detects_users_path(self, tmp_path: Path) -> None:
        py_file = tmp_path / "config.py"
        py_file.write_text(
            'ROOT = "/Users/alice/Code"\n'
        )  # test-literal-ok: intentionally tests /Users/ detection
        violations = _check_no_hardcoded_paths("myrepo", py_file, tmp_path)
        assert len(violations) == 1
        assert violations[0].principle_code == "ARCH-005"

    def test_skips_local_path_ok(self, tmp_path: Path) -> None:
        py_file = tmp_path / "config.py"
        py_file.write_text(
            'ROOT = "/Users/alice/Code"  # local-path-ok\n'
        )  # test-literal-ok: intentionally tests /Users/ with allowlist annotation
        violations = _check_no_hardcoded_paths("myrepo", py_file, tmp_path)
        assert violations == []


@pytest.mark.unit
class TestHandlerIntegration:
    def test_empty_target_dirs_returns_no_violations(self) -> None:
        handler = _make_handler()
        result = handler.handle(ArchInvariantLoopRequest(target_dirs=[]))
        assert result.violations == []
        assert result.invariants_evaluated == 5

    def test_nonexistent_dir_skipped(self) -> None:
        handler = _make_handler()
        result = handler.handle(
            ArchInvariantLoopRequest(target_dirs=["/nonexistent/path/repo"])
        )
        assert result.violations == []

    def test_detects_violation_in_real_file(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "bad_handler.py").write_text(
            'x = os.environ.get("KEY", "default_val")\n'
        )
        handler = _make_handler()
        result = handler.handle(
            ArchInvariantLoopRequest(
                target_dirs=[str(tmp_path)],
                invariant_ids=["ARCH-002"],
            )
        )
        assert len(result.violations) == 1
        assert result.violations[0].principle_code == "ARCH-002"
        assert result.invariants_evaluated == 1

    def test_summary_populated(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "module.py").write_text("event_bus = None\n")
        handler = _make_handler()
        result = handler.handle(
            ArchInvariantLoopRequest(
                target_dirs=[str(tmp_path)],
                invariant_ids=["ARCH-004"],
            )
        )
        assert result.summary["total_violations"] >= 1
        assert "ARCH-004" in result.summary["by_principle"]

    def test_severity_threshold_filters_below(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        # ARCH-002 produces WARNING severity
        (src / "module.py").write_text('x = os.environ.get("K", "v")\n')
        handler = _make_handler()
        result = handler.handle(
            ArchInvariantLoopRequest(
                target_dirs=[str(tmp_path)],
                invariant_ids=["ARCH-002"],
                severity_threshold="ERROR",
            )
        )
        assert result.violations == []
