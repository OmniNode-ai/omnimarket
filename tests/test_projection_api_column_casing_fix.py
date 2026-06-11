# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-path fix proof: projection-api SELECT column list is casing-safe.

Context (OMN-12941)
-------------------
The ``projection_overnight_readiness`` view materializes case-sensitive output
columns via quoted camelCase aliases (``... AS "overallStatus"``). PostgreSQL
preserves the case of an identifier only when it is double-quoted. The
projection-api previously built its SELECT column list with a bare
``", ".join(columns)``; because YAML strips the quotes from the contract's
``projection_api.columns`` values, the joined list emitted ``overallStatus``
unquoted, PostgreSQL folded it to ``overallstatus``, and ``overnight.v1``
returned 503 ``column "overallstatus" does not exist``.

The guard test ``test_overnight_readiness_view_column_casing.py`` (OMN-12941,
PR #1155) documented the defect against the committed view DDL + contract. This
test proves the *fix*: :func:`build_select_column_list` is the single column-list
builder for every projection-api read path, and it emits every case-sensitive
view alias double-quoted so the resulting SELECT resolves against the view.

No live DB: the helper is pure, and the contract/view DDL are parsed statically.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from omnimarket.projection.api_server import build_select_column_list

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_overnight"
)
_VIEW_SQL = (
    _NODE_DIR / "migrations" / "0001_create_overnight_readiness_projection_view.sql"
)
_CONTRACT = _NODE_DIR / "contract.yaml"


def _quoted_view_aliases(view_sql: str) -> set[str]:
    """Case-preserved output columns the view exposes via ``AS "<alias>"``."""
    return set(re.findall(r'AS\s+"([^"]+)"', view_sql))


def _overnight_projection_columns() -> tuple[str, ...]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    exposures = contract["projection_api"]["exposures"]
    return tuple(str(column) for column in exposures[0]["columns"])


# ---------------------------------------------------------------------------
# Helper unit behavior.
# ---------------------------------------------------------------------------


def test_star_passthrough() -> None:
    assert build_select_column_list(("*",)) == "*"


def test_lowercase_columns_are_not_quoted() -> None:
    assert (
        build_select_column_list(("dimensions", "latest_projection_updated_at"))
        == "dimensions, latest_projection_updated_at"
    )


def test_mixed_case_columns_are_quoted() -> None:
    assert (
        build_select_column_list(("overallStatus", "lastCheckedAt"))
        == '"overallStatus", "lastCheckedAt"'
    )


def test_already_quoted_columns_are_idempotent() -> None:
    assert build_select_column_list(('"overallStatus"',)) == '"overallStatus"'


def test_embedded_double_quote_is_escaped() -> None:
    assert build_select_column_list(('weird"name',)) == '"weird""name"'


# ---------------------------------------------------------------------------
# End-to-end read-path proof against the real overnight contract + view DDL.
# ---------------------------------------------------------------------------


def test_overnight_select_list_quotes_every_case_sensitive_view_alias() -> None:
    """The built SELECT list must quote every case-sensitive alias the view exposes.

    This is the invariant the read-path fix must satisfy: a column whose name in
    the view is case-sensitive (``overallStatus``, ``lastCheckedAt``) must appear
    double-quoted in the emitted SELECT list so PostgreSQL resolves it against
    the view instead of folding it to lowercase (the 503 cause).
    """
    columns = _overnight_projection_columns()
    view_aliases = _quoted_view_aliases(_VIEW_SQL.read_text(encoding="utf-8"))
    col_list = build_select_column_list(columns)

    case_sensitive_selected = {
        alias for alias in view_aliases if alias in {c.strip('"') for c in columns}
    }
    assert case_sensitive_selected, (
        "expected the overnight contract to select at least one case-sensitive "
        f"view alias; aliases={sorted(view_aliases)} columns={columns}"
    )
    for alias in case_sensitive_selected:
        # Must appear double-quoted (case-preserved), never as a bare identifier.
        assert f'"{alias}"' in col_list, (
            f"case-sensitive view alias {alias!r} must be quoted in the SELECT "
            f"list; got {col_list!r}"
        )
        assert not re.search(rf'(?<!")\b{re.escape(alias)}\b(?!")', col_list), (
            f"case-sensitive view alias {alias!r} must not appear unquoted "
            f"(the OMN-12941 503 defect); got {col_list!r}"
        )
