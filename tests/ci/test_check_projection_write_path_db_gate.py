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


@pytest.mark.unit
class TestPureFoldCarveOut:
    """OMN-20474: a node whose contract declares a pure fold owns no write path."""

    _HANDLER = "src/omnimarket/nodes/node_projection_fake/handlers/handler_fold.py"

    def _repo(self, tmp_path: Path, contract_text: str | None) -> Path:
        if contract_text is not None:
            node = tmp_path / "src" / "omnimarket" / "nodes" / "node_projection_fake"
            node.mkdir(parents=True)
            (node / "contract.yaml").write_text(contract_text, encoding="utf-8")
        return tmp_path

    def test_pure_contract_without_db_io_passes(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "descriptor:\n  purity: pure\n")
        assert evaluate([self._HANDLER], repo).passed

    def test_declared_db_io_is_still_a_write_path(self, tmp_path: Path) -> None:
        repo = self._repo(
            tmp_path, "descriptor:\n  purity: pure\ndb_io:\n  db_tables: []\n"
        )
        assert not evaluate([self._HANDLER], repo).passed

    def test_impure_contract_is_still_a_write_path(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "descriptor:\n  purity: impure\n")
        assert not evaluate([self._HANDLER], repo).passed

    def test_unreadable_or_missing_contract_fails_closed(self, tmp_path: Path) -> None:
        assert not evaluate([self._HANDLER], self._repo(tmp_path, None)).passed
        broken = tmp_path / "broken"
        broken.mkdir()
        assert not evaluate(
            [self._HANDLER], self._repo(broken, "descriptor: [unclosed\n")
        ).passed

    def test_the_real_judged_acceptance_fold_is_exempt(self) -> None:
        handler = (
            "src/omnimarket/nodes/node_projection_delegation_judged_acceptance/"
            "handlers/handler_projection_delegation_judged_acceptance.py"
        )
        repo_root = Path(__file__).resolve().parents[2]
        assert is_write_path_target(handler)
        assert evaluate([handler], repo_root).passed
