# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local store's delegation summary view, one step per revision.

OMN-20709 gave a local store the SQLite counterpart of migration 0050's
projection_delegation_summary. OMN-23169 is the counterpart of migration 0068,
which leaves rows typed experiment (synthetic runs) out of every total while
delegation_events, and so the Runs listing, keeps them.

Each revision is its own step: a store that has recorded one never runs it
again, so a later revision is a new tuple here, never an edit to an old SQL file.
The store's migration runner (SqliteDatabaseAdapter) records and runs the steps.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files

_PACKAGE = "omnimarket.nodes.node_projection_delegation"

DELEGATION_SUMMARY_VIEW_STEPS = (
    ("omn20709_delegation_summary_view", "sqlite/delegation_summary_view.sql"),
    (
        "omn23169_delegation_summary_excludes_experiment",
        "sqlite/delegation_summary_view_v2.sql",
    ),
)


def create_delegation_summary_view(conn: sqlite3.Connection, resource: str) -> None:
    """Drop the summary view and create the revision ``resource`` defines.

    Dropped first so a store that holds an older definition takes this one.
    """
    ddl = files(_PACKAGE).joinpath(resource).read_text(encoding="utf-8")
    conn.execute("DROP VIEW IF EXISTS projection_delegation_summary")
    conn.execute(ddl)
