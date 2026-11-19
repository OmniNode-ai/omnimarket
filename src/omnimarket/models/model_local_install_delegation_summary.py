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
    # OMN-24615 AC4, AC6: keeps NULL-data_source runs, counts only evaluated runs, names the model that ran.
    (
        "omn24615_delegation_summary_evaluated_runs",
        "sqlite/delegation_summary_view_v3.sql",
    ),
    # OMN-23413 AC51: the headline savings also keep NULL-data_source runs.
    (
        "omn23413_delegation_summary_counts_unclassified",
        "sqlite/delegation_summary_view_v4.sql",
    ),
    # OMN-24615 AC6: each byModel row serves the backends its runs were reached through.
    (
        "omn24615_delegation_summary_backends",
        "sqlite/delegation_summary_view_v5.sql",
    ),
)


def create_delegation_summary_view(conn: sqlite3.Connection, resource: str) -> None:
    """Drop the summary view and create the revision ``resource`` defines.

    Dropped first so a store that holds an older definition takes this one.
    """
    ddl = files(_PACKAGE).joinpath(resource).read_text(encoding="utf-8")
    conn.execute("DROP VIEW IF EXISTS projection_delegation_summary")
    conn.execute(ddl)


# The delegation views replaced in place by one store step each, in the order they run: (step, (view, SQL resource
# under the projection package), what the step did). OMN-21015 AC2: the model-routing view whose tiers list their
# models. OMN-21970: the per-day view. OMN-24615 AC4, AC6, AC7: revisions of the quality-gate, model-routing and
# per-day views (only evaluated runs are quality checks, per-model figures name the model that ran, and the per-day
# counts use the store's own markers). A later revision is a new step after these; an earlier step is never edited.
DELEGATION_VIEW_REPLACE_STEPS: tuple[tuple[str, tuple[str, str], str], ...] = (
    (
        "omn21015_model_routing_tier_models",
        (
            "projection_delegation_model_routing",
            "sqlite/delegation_model_routing_view_v2.sql",
        ),
        "listed each tier's models in the delegation model-routing view",
    ),
    (
        "omn21970_delegation_daily_view",
        ("projection_delegation_daily", "sqlite/delegation_daily_view.sql"),
        "created the per-day view",
    ),
    (
        "omn24615_quality_gate_evaluated_runs",
        (
            "projection_delegation_quality_gate",
            "sqlite/delegation_quality_gate_view_v2.sql",
        ),
        "counted only evaluated runs as quality checks",
    ),
    (
        "omn24615_model_routing_served_model",
        (
            "projection_delegation_model_routing",
            "sqlite/delegation_model_routing_view_v3.sql",
        ),
        "grouped model routing by the model that ran",
    ),
    (
        "omn24615_daily_store_markers",
        ("projection_delegation_daily", "sqlite/delegation_daily_view_v2.sql"),
        "counted unrecorded runs and unmeasured tokens from the store's markers",
    ),
    (
        "omn24615_model_routing_priced_spend",
        (
            "projection_delegation_model_routing",
            "sqlite/delegation_model_routing_view_v4.sql",
        ),
        "served each tier model's cost from a named price only, and each per-model row's backends",
    ),
    (
        "omn24615_quality_gate_backends",
        (
            "projection_delegation_quality_gate",
            "sqlite/delegation_quality_gate_view_v3.sql",
        ),
        "served the backends of each model's tokens-to-compliance row",
    ),
)
