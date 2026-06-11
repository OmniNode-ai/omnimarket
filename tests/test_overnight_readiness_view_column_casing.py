# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression guard: overnight readiness view ↔ projection-api column casing.

Context (OMN-12941)
-------------------
The ``projection_overnight_readiness`` view materializes a case-sensitive output
column by quoting a camelCase alias in its DDL:

    SELECT ... status.overall_status AS "overallStatus", ...

PostgreSQL preserves the case of a *double-quoted* identifier. An *unquoted*
reference to the same column is folded to lowercase, so a read query that selects
``overallStatus`` (no quotes) actually asks for ``overallstatus``, which does not
exist — yielding ``column "overallstatus" does not exist`` and a 503 from
``overnight.v1``.

The projection-api builds its SELECT column list with a bare
``", ".join(cfg.columns)`` (see ``omnimarket.projection.api_server._build_query``-
equivalent logic). ``cfg.columns`` comes verbatim from the contract's
``projection_api.columns`` list, and YAML strips the quotes from a value written
as ``- "overallStatus"`` — so the joined column list emits ``overallStatus``
unquoted. That is the OMN-12941 read-path defect.

These tests codify the casing contract that the read path depends on, with **no
live DB dependency**: they statically parse the committed view DDL and the node
contract. They fail closed if either side drifts (e.g. the view alias is
renamed, the quotes are dropped, or the contract column list diverges from what
the view actually materializes). They intentionally do **not** edit the gated
migration file ``0001_create_overnight_readiness_projection_view.sql`` — the
SQL/view fix stays separately gated and ticket-only.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Paths to the committed, statically-parseable artifacts.
# ---------------------------------------------------------------------------

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

# A bare SQL identifier is case-insensitive (folded to lowercase by PostgreSQL)
# only when it is all-lowercase. Any identifier containing an uppercase letter
# requires double-quoting to survive a round trip through PostgreSQL.
_NEEDS_QUOTING = re.compile(r"[A-Z]")


def _quoted_view_aliases(view_sql: str) -> set[str]:
    """Extract case-preserved output column aliases from the view DDL.

    Returns the set of identifiers the view exposes via ``AS "<alias>"`` — the
    only columns whose casing PostgreSQL preserves and that therefore must be
    quoted by any reader.
    """
    return set(re.findall(r'AS\s+"([^"]+)"', view_sql))


def _projection_columns(contract: dict[str, object]) -> list[str]:
    """Return the projection-api column list declared by the overnight contract.

    The contract uses the explicit ``exposures`` form; this reads the first
    (and only) exposure's ``columns`` list, mirroring how
    ``build_projection_topic_map`` parses it.
    """
    projection_api = contract["projection_api"]
    assert isinstance(projection_api, dict)
    exposures = projection_api["exposures"]
    assert isinstance(exposures, list)
    assert exposures
    columns = exposures[0]["columns"]
    assert isinstance(columns, list)
    return [str(column) for column in columns]


def _read_view_sql() -> str:
    return _VIEW_SQL.read_text(encoding="utf-8")


def _read_contract() -> dict[str, object]:
    return yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Artifact-existence sanity (fail loudly if the files move).
# ---------------------------------------------------------------------------


def test_view_and_contract_artifacts_exist() -> None:
    assert _VIEW_SQL.is_file(), f"missing view DDL: {_VIEW_SQL}"
    assert _CONTRACT.is_file(), f"missing contract: {_CONTRACT}"


# ---------------------------------------------------------------------------
# The defect that OMN-12941 codifies.
# ---------------------------------------------------------------------------


def test_view_exposes_case_sensitive_overall_status_alias() -> None:
    """The view must expose the readiness status as quoted camelCase.

    The readiness-gate widget reads ``overallStatus`` (camelCase). The view is
    the single source of that name; if it stops quoting the alias the casing
    contract silently breaks. Pin the exact alias so a rename is caught here.
    """
    aliases = _quoted_view_aliases(_read_view_sql())
    assert "overallStatus" in aliases, (
        "view must materialize the readiness status as a quoted "
        f'"overallStatus" alias; found quoted aliases: {sorted(aliases)}'
    )


def test_contract_columns_match_view_case_sensitive_aliases() -> None:
    """Every case-preserved view alias must appear verbatim in the contract.

    The projection-api selects exactly ``projection_api.columns``. If the
    contract declares a casing that differs from the view's quoted alias (e.g.
    ``overall_status`` vs the view's ``overallStatus``), the read query targets a
    column the view does not expose. Pin the two together.
    """
    view_aliases = _quoted_view_aliases(_read_view_sql())
    declared = set(_projection_columns(_read_contract()))
    missing = view_aliases - declared
    assert not missing, (
        "contract projection_api.columns must declare every case-sensitive view "
        f"alias verbatim; missing from contract: {sorted(missing)} "
        f"(declared: {sorted(declared)})"
    )


def test_mixed_case_columns_resolve_through_built_select_list() -> None:
    """Regression guard for the OMN-12941 read-path defect.

    Reproduce the projection-api column-list build (``", ".join(columns)``) and
    assert that every mixed-case column survives as a case-sensitive,
    double-quoted identifier in the emitted SELECT list. A mixed-case column that
    appears *unquoted* in the joined list is the exact failure mode that makes
    PostgreSQL fold ``overallStatus`` → ``overallstatus`` and raise
    ``column does not exist`` — the 503 this ticket tracks.

    This test pins the contract that the read-path fix must satisfy: mixed-case
    projection columns are emitted quoted. It does not assert *how* the fix is
    implemented (quote-in-query vs. snake_case rename), only that the resulting
    column list resolves against the view's case-sensitive aliases.
    """
    columns = _projection_columns(_read_contract())
    view_aliases = _quoted_view_aliases(_read_view_sql())

    # ------------------------------------------------------------------
    # Document the defect: the production column-list build is a bare
    # ``", ".join(columns)`` (omnimarket.projection.api_server, the SELECT
    # column-list expression). With the contract values, that emits the
    # mixed-case ``overallStatus`` UNQUOTED — the exact 503 cause. Pin this so
    # the regression guard fails loudly if someone "fixes" it by silently
    # dropping the camelCase column instead of quoting it.
    # ------------------------------------------------------------------
    naive_select_list = "*" if tuple(columns) == ("*",) else ", ".join(columns)
    case_sensitive_selected = {
        alias
        for alias in view_aliases
        if alias in {column.strip('"') for column in columns}
    }
    assert case_sensitive_selected, (
        "expected the overnight contract to select at least one case-sensitive "
        f"view alias; aliases={sorted(view_aliases)} columns={columns}"
    )
    for alias in case_sensitive_selected:
        # The naive (current) build leaves the mixed-case column unquoted —
        # this is the read-path defect OMN-12941 tracks. Asserting it here keeps
        # the guard honest about current behavior; the casing-safe build below
        # is what the fix must produce.
        assert re.search(rf'(?<!")\b{re.escape(alias)}\b(?!")', naive_select_list), (
            "expected the naive join to emit the case-sensitive column "
            f"{alias!r} unquoted (the defect); list={naive_select_list!r}"
        )

    # Build the SELECT column list the way the projection-api does today, but
    # apply the casing-safe rule: any column whose canonical name in the view is
    # case-sensitive must be emitted double-quoted.
    def _emit(column: str) -> str:
        stripped = column.strip('"')
        if stripped in view_aliases or _NEEDS_QUOTING.search(stripped):
            return f'"{stripped}"'
        return stripped

    safe_select_list = ", ".join(_emit(column) for column in columns)

    # Every case-sensitive view alias the contract selects must appear quoted in
    # the built list; never bare (which would fold to lowercase and 404).
    for alias in view_aliases:
        if alias not in {column.strip('"') for column in columns}:
            continue
        assert f'"{alias}"' in safe_select_list, (
            f"case-sensitive column {alias!r} must be emitted double-quoted in "
            f"the SELECT list, got: {safe_select_list!r}"
        )
        # Guard against the defect signature: the bare, unquoted token must not
        # stand alone as a selected column.
        bare_token = re.compile(rf'(?<!")\b{re.escape(alias)}\b(?!")')
        assert not bare_token.search(safe_select_list), (
            f"case-sensitive column {alias!r} appears unquoted in the SELECT "
            f"list — PostgreSQL would fold it to {alias.lower()!r} and 503; "
            f"list: {safe_select_list!r}"
        )
