# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure grader of the C30 customer dashboard path (OMN-26044).

A customer starts the dashboard with ``onex dashboard start``: the session token lands in the local credential store (an
owner-only file), and a Bearer read on loopback shows the run. A dashboard started bare keeps its one-time token to itself,
so nobody can learn it: that is an absent instrument (PRECONDITION-ABSENT with the reason), never a pass and never a fail of
the product. Once a token is learnable, every clause must hold.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    ModelCheckClause,
    ModelCheckPrecondition,
    ModelDashboardObservation,
)

TOKEN_UNLEARNABLE = (
    "the dashboard token is not in the local credential store: a bare `onex dashboard` keeps its one-time token to itself and "
    "no check can learn it; start it the way a customer does, with `onex dashboard start`"
)
REFUSED = (401, 403)


def _clause(name: str, ok: bool, detail: str) -> ModelCheckClause:
    return ModelCheckClause(name=name, ok=ok, detail=detail)


def grade_c30_dashboard(
    obs: ModelDashboardObservation,
) -> tuple[tuple[ModelCheckClause, ...], tuple[ModelCheckPrecondition, ...]]:
    # An install or setup that did not complete is the customer path failing, never an absent instrument.
    if obs.install_rc not in (0, None) or obs.setup_rc not in (0, None):
        return (
            (
                _clause(
                    "dashboard_started_by_customer_command",
                    False,
                    f"install exited {obs.install_rc} and setup exited {obs.setup_rc}, so no dashboard could be started",
                ),
            ),
            (),
        )
    learnable = ModelCheckPrecondition(
        name="dashboard_token_learnable",
        present=obs.token_in_store,
        detail="the dashboard's session token is in the local credential store"
        if obs.token_in_store
        else TOKEN_UNLEARNABLE,
    )
    if not obs.token_in_store:
        return (), (learnable,)
    status = obs.projections_status
    return (
        _clause(
            "dashboard_started_by_customer_command",
            obs.start_rc == 0,
            "onex dashboard start exited 0"
            if obs.start_rc == 0
            else f"onex dashboard start exited {obs.start_rc}",
        ),
        _clause(
            "store_token_owner_only",
            obs.store_owner_only,
            "the credential store file is readable by its owner alone"
            if obs.store_owner_only
            else "the credential store file is readable beyond its owner",
        ),
        _clause(
            "delegation_ran_once",
            obs.delegation_rc == 0,
            "one delegation exited 0"
            if obs.delegation_rc == 0
            else f"the delegation exited {obs.delegation_rc}",
        ),
        _clause(
            "projections_read_with_store_token",
            status == 200,
            "GET /projections on loopback with the store token as Bearer answered 200"
            if status == 200
            else f"GET /projections with the store token answered {status}",
        ),
        _clause(
            "run_visible_on_projections",
            obs.run_visible,
            f"the run {obs.delegation_correlation_id} is on the projection read"
            if obs.run_visible
            else f"the run {obs.delegation_correlation_id} is not on the projection read",
        ),
        _clause(
            "wrong_token_refused",
            obs.wrong_token_status in REFUSED,
            f"a wrong Bearer token answered {obs.wrong_token_status}",
        ),
        _clause(
            "absent_token_refused",
            obs.no_token_status in REFUSED,
            f"a read with no token answered {obs.no_token_status}",
        ),
    ), (learnable,)
