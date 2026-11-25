"""The projection table reader stays within its canonical-file-shape baseline (OMN-24562).

``src/omnimarket/projection/table_reader.py`` is a baselined file: the canonical
file shape ratchet (OMN-20304) refuses a change that grows its code-line count
past the merge base's, which was 914. The windowed-read fix keeps the file at or
under that count by moving the catalogue SQL into ``projection_table_sql``.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.handlers.projection_table_sql import (
    UNIQUE_KEY_CATALOGUE_SQL,
    WINDOW_ORDER_ALIAS,
    serves_column,
)
from omnimarket.projection.models import ProjectionTableConfig

_MERGE_BASE_CODE_LINES = 914
_READER = (
    Path(__file__).resolve().parents[1] / "src/omnimarket/projection/table_reader.py"
)


def _code_lines(text: str) -> int:
    return sum(
        1
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_table_reader_does_not_grow_past_the_merge_base() -> None:
    assert _code_lines(_READER.read_text()) <= _MERGE_BASE_CODE_LINES


def test_code_line_counter_counts_code_only() -> None:
    assert _code_lines("# note\n\nx = 1\n    y = 2\n") == 2


def test_unique_key_catalogue_sql_is_one_bound_relation_read() -> None:
    assert "pg_get_indexdef" in UNIQUE_KEY_CATALOGUE_SQL
    assert UNIQUE_KEY_CATALOGUE_SQL.count("$1") == 1
    assert UNIQUE_KEY_CATALOGUE_SQL.endswith("i.indexprs IS NULL")


def test_serves_column_reads_the_declared_columns() -> None:
    def cfg(columns: tuple[str, ...]) -> ProjectionTableConfig:
        return ProjectionTableConfig.model_construct(columns=columns)

    assert serves_column(cfg(("a", "updated_at")), "updated_at")
    assert serves_column(cfg(("a", '"updated_at"')), "updated_at")
    assert not serves_column(cfg(("a", "b")), "updated_at")
    assert serves_column(cfg(("*",)), "updated_at")
    assert WINDOW_ORDER_ALIAS == '"__window_instant"'
