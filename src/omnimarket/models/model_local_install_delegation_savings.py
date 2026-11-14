# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local store's delegation savings view, one step per revision.

The SQLite counterpart of node_projection_savings migration 090's savings view
serves the local delegation.savings.v1 exposure. Revisions in order: the view
with each run's stored call provenance; unpriced_call_count appended (the
counterpart of migration 096); savings money restricted to measured runs with
each basis counted; and a run with no stored cost served with a null session
cost instead of 0, so the page can say the cost was not measured.

Each revision is its own step: a store that has recorded one never runs it
again, so a later revision is a new tuple here, never an edit to an old SQL file.
The store's migration runner (SqliteDatabaseAdapter) records and runs the steps
in this order.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files

_PACKAGE = "omnimarket.nodes.node_projection_savings"

DELEGATION_SAVINGS_VIEW_STEPS = (
    (
        "omn20008_delegation_savings_view",
        "sqlite/delegation_savings_view.sql",
        "created the delegation savings view",
    ),
    (
        "omn20717_delegation_savings_unpriced_view",
        "sqlite/delegation_savings_view_unpriced.sql",
        "re-created the delegation savings view with its unpriced-call count",
    ),
    (
        "omn21206_delegation_savings_measured_view",
        "sqlite/delegation_savings_view_measured.sql",
        "restricted savings money to measured runs and counted each basis",
    ),
    (
        "omn24224_delegation_savings_null_cost_view",
        "sqlite/delegation_savings_view_null_cost.sql",
        "served a run with no stored cost as a null session cost",
    ),
)


def create_delegation_savings_view(conn: sqlite3.Connection, resource: str) -> None:
    """Drop the savings view and create the revision ``resource`` defines.

    Dropped first so a store that holds an older definition takes this one.
    """
    ddl = files(_PACKAGE).joinpath(resource).read_text(encoding="utf-8")
    conn.execute("DROP VIEW IF EXISTS projection_delegation_savings")
    conn.execute(ddl)
