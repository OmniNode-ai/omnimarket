# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the delegation env read scanner.

Verifies that the scanner:
- finds known os.environ/os.getenv violations in delegation modules
- ignores test fixtures and non-delegation modules
- operates in report mode without failing CI

Ticket: OMN-10917
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

scripts_ci_dir = Path(__file__).parent.parent / "scripts" / "ci"
sys.path.insert(0, str(scripts_ci_dir))

from check_delegation_env_reads import (  # noqa: E402
    ScanResult,
    _find_env_calls_in_source,
    _is_allowlisted,
    _is_delegation_module,
    scan_delegation_modules,
)


class TestIsDelegationModule:
    def test_delegation_orchestrator(self) -> None:
        assert _is_delegation_module(
            "src/omnimarket/nodes/node_delegation_orchestrator/handlers/h.py"
        )

    def test_delegation_routing_reducer(self) -> None:
        assert _is_delegation_module(
            "src/omnimarket/nodes/node_delegation_routing_reducer/handlers/h.py"
        )

    def test_delegate_skill_orchestrator(self) -> None:
        assert _is_delegation_module(
            "src/omnimarket/nodes/node_delegate_skill_orchestrator/node.py"
        )

    def test_projection_delegation(self) -> None:
        assert _is_delegation_module(
            "src/omnimarket/nodes/node_projection_delegation/handlers/h.py"
        )

    def test_adapters_llm_bifrost(self) -> None:
        assert _is_delegation_module(
            "src/omnimarket/adapters/llm/bifrost/config_loader.py"
        )

    def test_non_delegation_module_excluded(self) -> None:
        assert not _is_delegation_module(
            "src/omnimarket/nodes/node_ab_compare_reducer/node.py"
        )

    def test_pr_review_node_excluded(self) -> None:
        assert not _is_delegation_module(
            "src/omnimarket/nodes/node_pr_review_orchestrator/node.py"
        )


class TestIsAllowlisted:
    def test_test_file_allowlisted(self) -> None:
        assert _is_allowlisted(
            "src/omnimarket/nodes/node_delegation_orchestrator/tests/test_foo.py"
        )

    def test_fixtures_allowlisted(self) -> None:
        assert _is_allowlisted("tests/fixtures/delegation_fixture.py")

    def test_conftest_allowlisted(self) -> None:
        assert _is_allowlisted("tests/conftest.py")

    def test_regular_handler_not_allowlisted(self) -> None:
        assert not _is_allowlisted(
            "src/omnimarket/adapters/llm/bifrost/config_loader.py"
        )


class TestFindEnvCallsInSource:
    def test_detects_os_environ_get(self) -> None:
        source = 'import os\nvalue = os.environ.get("KEY", "")\n'
        violations = _find_env_calls_in_source(source, "test.py")
        assert len(violations) == 1
        assert "os.environ.get" in violations[0]
        assert "test.py:2" in violations[0]

    def test_detects_os_getenv(self) -> None:
        source = 'import os\nvalue = os.getenv("KEY")\n'
        violations = _find_env_calls_in_source(source, "test.py")
        assert len(violations) == 1
        assert "os.getenv" in violations[0]

    def test_detects_os_environ_subscript(self) -> None:
        source = 'import os\nvalue = os.environ["KEY"]\n'
        violations = _find_env_calls_in_source(source, "test.py")
        assert len(violations) == 1
        assert "os.environ" in violations[0]

    def test_skips_onex_flag_exempt(self) -> None:
        source = (
            'import os\nvalue = os.getenv("KEY")  # ONEX_FLAG_EXEMPT: activation gate\n'
        )
        violations = _find_env_calls_in_source(source, "test.py")
        assert len(violations) == 0

    def test_skips_onex_exclude(self) -> None:
        source = (
            'import os\nvalue = os.environ.get("KEY")  # ONEX_EXCLUDE: archive port\n'
        )
        violations = _find_env_calls_in_source(source, "test.py")
        assert len(violations) == 0

    def test_no_false_positive_on_attribute_not_os(self) -> None:
        source = 'value = config.environ.get("KEY")\n'
        violations = _find_env_calls_in_source(source, "test.py")
        assert len(violations) == 0

    def test_multiple_violations_deduplicated_per_line(self) -> None:
        # os.environ.get produces one violation per line, not two
        source = 'import os\nvalue = os.environ.get("KEY", "")\n'
        violations = _find_env_calls_in_source(source, "test.py")
        assert len(violations) == 1

    def test_multiple_lines_detected(self) -> None:
        source = (
            "import os\n"
            'url = os.environ.get("LLM_URL", "")\n'
            'key = os.getenv("API_KEY")\n'
        )
        violations = _find_env_calls_in_source(source, "test.py")
        assert len(violations) == 2


@pytest.mark.unit
class TestScanDelegationModules:
    def test_scanner_finds_known_violations(self) -> None:
        repo_root = Path(__file__).parent.parent
        result = scan_delegation_modules(repo_root=repo_root, mode="report")
        assert result.scanned_files > 0
        assert result.report_generated

    def test_scanner_finds_violations_in_repo(self) -> None:
        """Known env reads exist in delegation modules — scanner must detect them."""
        repo_root = Path(__file__).parent.parent
        result = scan_delegation_modules(repo_root=repo_root, mode="report")
        assert len(result.violations) > 0, (
            "Expected known env reads in delegation modules — "
            "check node_delegation_routing_reducer and adapters/llm/bifrost/"
        )

    def test_report_mode_does_not_fail(self) -> None:
        repo_root = Path(__file__).parent.parent
        result = scan_delegation_modules(repo_root=repo_root, mode="report")
        assert result.report_generated

    def test_scan_result_is_dataclass(self) -> None:
        result = ScanResult()
        assert result.scanned_files == 0
        assert result.violations == []
        assert result.report_generated is False

    def test_scanner_ignores_non_delegation_files(self, tmp_path: Path) -> None:
        """Files outside delegation module patterns must not appear in violations."""
        src = tmp_path / "src" / "omnimarket" / "nodes" / "node_ab_compare_reducer"
        src.mkdir(parents=True)
        (src / "handler.py").write_text('import os\nos.getenv("KEY")\n')
        (tmp_path / ".git").mkdir()

        result = scan_delegation_modules(repo_root=tmp_path, mode="report")
        assert result.scanned_files == 0
        assert result.violations == []
        assert result.report_generated

    def test_scanner_ignores_test_files_in_delegation_paths(
        self, tmp_path: Path
    ) -> None:
        """Test files inside delegation module directories must be skipped."""
        src = (
            tmp_path
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_delegation_orchestrator"
            / "tests"
        )
        src.mkdir(parents=True)
        (src / "test_handler.py").write_text('import os\nos.getenv("KEY")\n')
        (tmp_path / ".git").mkdir()

        result = scan_delegation_modules(repo_root=tmp_path, mode="report")
        assert result.scanned_files == 0
        assert result.violations == []
