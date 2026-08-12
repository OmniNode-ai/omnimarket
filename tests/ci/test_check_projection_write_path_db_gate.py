# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the projection write-path real-DB proof gate (OMN-15909).

Encodes the regression the gate exists to prevent: a diff that touches a
projection write-path surface (``node_projection_*/handlers/**`` or
``projection/runner.py``) but ships no accompanying real-Postgres
``@pytest.mark.integration`` test must turn RED (case b/c), while the same
diff WITH a real-DB integration test change must PASS (case d/e) -- and a
diff untouching any write-path surface always passes regardless of test
coverage (case a).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_projection_write_path_db_gate import (
    INTEGRATION_MARKER,
    REAL_DB_SIGNAL,
    evaluate,
    is_real_db_integration_test_change,
    is_write_path_target,
    selftest,
)


@pytest.mark.unit
def test_selftest_passes() -> None:
    """The gate's own encoded RED/GREEN cases must hold -- this is the
    regression guard against the gate itself silently stopping to enforce."""
    assert selftest() == 0


@pytest.mark.unit
class TestIsWritePathTarget:
    def test_matches_delegation_handler(self) -> None:
        assert is_write_path_target(
            "src/omnimarket/nodes/node_projection_delegation/handlers/"
            "handler_delegation.py"
        )

    def test_matches_shared_runner(self) -> None:
        assert is_write_path_target("src/omnimarket/projection/runner.py")

    def test_does_not_match_unrelated_node(self) -> None:
        assert not is_write_path_target(
            "src/omnimarket/nodes/node_other/handlers/handler_x.py"
        )

    def test_does_not_match_migrations(self) -> None:
        assert not is_write_path_target(
            "src/omnimarket/nodes/node_projection_delegation/migrations/0031_new.sql"
        )

    def test_does_not_match_non_handlers_module(self) -> None:
        assert not is_write_path_target(
            "src/omnimarket/nodes/node_projection_delegation/models/model_x.py"
        )

    def test_does_not_match_test_file(self) -> None:
        assert not is_write_path_target(
            "tests/test_omn15909_real_postgres_projection_write_path_gate.py"
        )


@pytest.mark.unit
class TestIsRealDbIntegrationTestChange:
    def test_recognises_this_tickets_own_gate_test(self, tmp_path: Path) -> None:
        repo_root = tmp_path
        rel = "tests/test_omn15909_real_postgres_projection_write_path_gate.py"
        target = repo_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"{INTEGRATION_MARKER}\nasync def test_x():\n"
            f"    import os; os.environ['{REAL_DB_SIGNAL}_HOST']\n",
            encoding="utf-8",
        )
        assert is_real_db_integration_test_change(rel, repo_root)

    def test_rejects_non_tests_path(self, tmp_path: Path) -> None:
        rel = "src/omnimarket/foo.py"
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{INTEGRATION_MARKER}\n{REAL_DB_SIGNAL}\n", encoding="utf-8")
        assert not is_real_db_integration_test_change(rel, tmp_path)

    def test_rejects_marker_only(self, tmp_path: Path) -> None:
        rel = "tests/test_marker_only.py"
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{INTEGRATION_MARKER}\n", encoding="utf-8")
        assert not is_real_db_integration_test_change(rel, tmp_path)

    def test_rejects_signal_only(self, tmp_path: Path) -> None:
        rel = "tests/test_signal_only.py"
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{REAL_DB_SIGNAL}\n", encoding="utf-8")
        assert not is_real_db_integration_test_change(rel, tmp_path)

    def test_rejects_missing_file(self, tmp_path: Path) -> None:
        assert not is_real_db_integration_test_change("tests/test_deleted.py", tmp_path)


@pytest.mark.unit
class TestEvaluate:
    def test_no_write_path_changes_passes_with_no_tests(self, tmp_path: Path) -> None:
        result = evaluate(["src/omnimarket/nodes/node_other/handlers/x.py"], tmp_path)
        assert result.passed
        assert result.write_path_targets == []

    def test_write_path_change_without_integration_test_fails(
        self, tmp_path: Path
    ) -> None:
        result = evaluate(
            [
                "src/omnimarket/nodes/node_projection_delegation/handlers/"
                "handler_delegation.py"
            ],
            tmp_path,
        )
        assert not result.passed

    def test_write_path_change_with_integration_test_passes(
        self, tmp_path: Path
    ) -> None:
        target_rel = "tests/test_real_db_gate.py"
        target = tmp_path / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{INTEGRATION_MARKER}\n{REAL_DB_SIGNAL}\n", encoding="utf-8")
        result = evaluate(
            [
                "src/omnimarket/nodes/node_projection_delegation/handlers/"
                "handler_delegation.py",
                target_rel,
            ],
            tmp_path,
        )
        assert result.passed
        assert result.covering_tests == [target_rel]
