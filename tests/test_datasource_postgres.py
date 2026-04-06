"""Tests for PostgresDataSource adapter.

Unit tests mock psycopg2 — no real DB needed.
Integration tests (marked @pytest.mark.integration) connect to .201:5436.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_data_verification.handlers.datasource_postgres import (
    PostgresDataSource,
)
from omnimarket.nodes.node_data_verification.handlers.handler_data_verification import (
    HandlerDataVerification,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_start_command import (
    ModelDataVerificationStartCommand,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_state import (
    EnumVerificationStatus,
)

# ---------------------------------------------------------------------------
# Unit tests (mocked psycopg2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPostgresDataSourceUnit:
    def test_raises_without_dsn(self) -> None:
        """Should raise RuntimeError if no DSN is provided and env var is unset."""
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(RuntimeError, match="OMNIDASH_ANALYTICS_DB_URL"),
        ):
            PostgresDataSource(dsn="")

    def test_get_row_count(self) -> None:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (42,)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.closed = False

        with patch("psycopg2.connect", return_value=mock_conn):
            ds = PostgresDataSource(dsn="postgresql://test:test@localhost/test")
            ds._conn = mock_conn
            count = ds.get_row_count("session_outcomes")

        assert count == 42

    def test_get_columns(self) -> None:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("session_id",),
            ("outcome",),
            ("emitted_at",),
        ]
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.closed = False

        with patch("psycopg2.connect", return_value=mock_conn):
            ds = PostgresDataSource(dsn="postgresql://test:test@localhost/test")
            ds._conn = mock_conn
            cols = ds.get_columns("session_outcomes")

        assert cols == ["session_id", "outcome", "emitted_at"]

    def test_get_sample_rows(self) -> None:
        mock_conn = MagicMock()
        mock_dict_cursor = MagicMock()
        mock_dict_cursor.fetchall.return_value = [
            {"session_id": "sess-1", "outcome": "success", "emitted_at": "2026-04-06"},
            {"session_id": "sess-2", "outcome": "failure", "emitted_at": "2026-04-05"},
        ]
        mock_dict_cursor.__enter__ = MagicMock(return_value=mock_dict_cursor)
        mock_dict_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_dict_cursor
        mock_conn.closed = False

        with patch("psycopg2.connect", return_value=mock_conn):
            ds = PostgresDataSource(dsn="postgresql://test:test@localhost/test")
            ds._conn = mock_conn
            rows = ds.get_sample_rows("session_outcomes", 2)

        assert len(rows) == 2
        assert rows[0]["session_id"] == "sess-1"
        assert rows[1]["outcome"] == "failure"

    def test_none_values_become_empty_string(self) -> None:
        mock_conn = MagicMock()
        mock_dict_cursor = MagicMock()
        mock_dict_cursor.fetchall.return_value = [
            {"id": "abc", "name": None},
        ]
        mock_dict_cursor.__enter__ = MagicMock(return_value=mock_dict_cursor)
        mock_dict_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_dict_cursor
        mock_conn.closed = False

        with patch("psycopg2.connect", return_value=mock_conn):
            ds = PostgresDataSource(dsn="postgresql://test:test@localhost/test")
            ds._conn = mock_conn
            rows = ds.get_sample_rows("test_table", 1)

        assert rows[0]["name"] == ""

    def test_handler_works_with_postgres_datasource(self) -> None:
        """Verify HandlerDataVerification accepts PostgresDataSource (protocol check)."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (5,)
        mock_cursor.fetchall.return_value = [("id",), ("name",)]
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_dict_cursor = MagicMock()
        mock_dict_cursor.fetchall.return_value = [
            {"id": str(uuid4()), "name": "test"},
        ]
        mock_dict_cursor.__enter__ = MagicMock(return_value=mock_dict_cursor)
        mock_dict_cursor.__exit__ = MagicMock(return_value=False)

        call_count = 0

        def cursor_factory(**kwargs):
            nonlocal call_count
            call_count += 1
            if "cursor_factory" in kwargs:
                return mock_dict_cursor
            return mock_cursor

        mock_conn.cursor = MagicMock(side_effect=cursor_factory)
        mock_conn.closed = False

        with patch("psycopg2.connect", return_value=mock_conn):
            ds = PostgresDataSource(dsn="postgresql://test:test@localhost/test")
            ds._conn = mock_conn

            handler = HandlerDataVerification()
            command = ModelDataVerificationStartCommand(
                table_name="session_outcomes",
                expected_columns=["id", "name"],
                unique_columns=["id"],
                uuid_columns=["id"],
                correlation_id=str(uuid4()),
                requested_at=datetime.now(tz=UTC),
            )

            result, completed = handler.run_verification(command, ds)

        assert result.total_rows == 5
        assert completed.table_name == "session_outcomes"


# ---------------------------------------------------------------------------
# Integration tests (require real .201 DB -- skip in CI)
# ---------------------------------------------------------------------------

_HAS_DB = bool(os.environ.get("OMNIDASH_ANALYTICS_DB_URL"))


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_DB, reason="OMNIDASH_ANALYTICS_DB_URL not set")
class TestPostgresDataSourceIntegration:
    """Integration tests that hit the real omnidash_analytics DB on .201."""

    def _make_ds(self) -> PostgresDataSource:
        return PostgresDataSource()

    def test_connect_and_get_row_count(self) -> None:
        ds = self._make_ds()
        try:
            count = ds.get_row_count("session_outcomes")
            assert isinstance(count, int)
            assert count >= 0
        finally:
            ds.close()

    def test_get_columns(self) -> None:
        ds = self._make_ds()
        try:
            cols = ds.get_columns("session_outcomes")
            assert isinstance(cols, list)
            assert "session_id" in cols
        finally:
            ds.close()

    def test_get_sample_rows(self) -> None:
        ds = self._make_ds()
        try:
            rows = ds.get_sample_rows("session_outcomes", 3)
            assert isinstance(rows, list)
            for row in rows:
                assert isinstance(row, dict)
                # All values should be strings
                for v in row.values():
                    assert isinstance(v, str)
        finally:
            ds.close()

    def test_full_verification_against_real_db(self) -> None:
        """Run HandlerDataVerification with PostgresDataSource on real data."""
        ds = self._make_ds()
        try:
            handler = HandlerDataVerification()
            command = ModelDataVerificationStartCommand(
                table_name="session_outcomes",
                expected_columns=["session_id", "outcome"],
                unique_columns=["session_id"],
                uuid_columns=[],
                min_rows=1,
                sample_size=5,
                correlation_id=str(uuid4()),
                requested_at=datetime.now(tz=UTC),
            )

            result, completed = handler.run_verification(command, ds)

            assert result.status in (
                EnumVerificationStatus.PASS,
                EnumVerificationStatus.PARTIAL,
                EnumVerificationStatus.FAIL,
            )
            assert completed.table_name == "session_outcomes"
            assert result.total_rows >= 0
        finally:
            ds.close()
