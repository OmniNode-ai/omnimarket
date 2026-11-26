# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""C31 producer -- the six local pages render served exposures and reconcile (OMN-19989).

WHAT C31 CLAIMS
    "The six local pages -- Overview, Runs, Workflow, Usage, Credentials, API
    Keys -- each render served exposures and compute nothing, and the figures
    reconcile against the CLI on the same store."

WHAT CAN BE GRADED TODAY, AND WHY IT IS WORTH GRADING
    `onex metering` already exists. Its JSON payload is the contract the
    dashboard's savings figure has to agree with, and the dashboard cannot be
    made to agree with a contract that drifts. So the clauses below grade the
    CONTRACT now -- before the pages exist -- which is the half of C31 that
    would otherwise be discovered broken at the demo.

    Three of them encode rules the model already states in prose and nothing
    enforced: savings is None and never zero when unmeasured; a savings figure
    is never reported without the baseline it was derived against; and
    `savings_usd` is a statement about `runs_measured`, not `runs_total`.

THE PAGE HALF (OMN-19984)
    The probe mints an identity with `onex local init` in a scratch HOME, seeds
    one MEASURED fixture delegation under it, starts the real `onex dashboard`
    on that HOME and reads two served exposures: Runs
    (`delegation.decisions.v1`), which must hold the fixture's correlation id
    with its model and tokens, and the one Overview's Savings widget binds:
    `metering-summary.v1` (OMN-19980 Step B, omnidash#364), whose `all` window
    row's `savings_usd` must equal `onex metering --json`'s `savings_usd` to the
    cent, with the same baseline model string. Null on either side fails; it is
    never read as agreement. (`cost.savings-overview.v1` is not Overview's
    binding and local SQLite mode never serves it.)

    From omnimarket 0.4.301 `onex metering` reads the STORED summary row
    (omnimarket#3407), so the probe refreshes the summary once after seeding,
    through `refresh_metering_summary`, before it reads either side.

WHAT IS GATED
    An exposure the server refuses because the local store does not serve it
    yet (`projection_column_missing`, `projection_table_missing`,
    `not_yet_bus_backed`) is a missing precondition naming the ticket that
    serves it, not a failed clause. Read 2026-10-04 against omnimarket 0.4.291,
    both exposures answered 503 that way, so this probe exits 2 --
    PRECONDITION-ABSENT -- which is red on the board and distinguishable in
    the log from a clause that was graded and failed. It turns green by itself
    when the surfaces land; nobody has to remember to edit it.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_c31_local_dashboard_parity_probe_effect.handlers.probe_base import (
    FIXTURE_CORRELATION_ID,
    Clause,
    ExposureRead,
    Precondition,
    ProbeResult,
    local_tenant_identity,
    onex_cli,
    onex_metering_json,
    onex_subcommand,
    read_exposure,
    running_dashboard,
    seed_fixture_delegation,
)

CRITERION = "C31"

#: Fields the dashboard's savings and usage widgets read. A rename here is a
#: silent break of every figure on Overview and Usage, which is exactly why the
#: producer pins the names rather than trusting them.
REQUIRED_SUMMARY_FIELDS = (
    "window",
    "runs_total",
    "runs_measured",
    "runs_unknown_tokens",
    "runs_unknown_spend",
    "tokens_in",
    "tokens_out",
    "spend_usd",
    "counterfactual_usd",
    "savings_usd",
    "baseline_state",
    "baseline",
)
REQUIRED_BASELINE_FIELDS = ("model", "pricing_manifest_version")
DECISIONS_TOPIC = "onex.snapshot.projection.delegation.decisions.v1"
#: The exposure Overview's Savings widget binds (OMN-19980 Step B, omnidash#364):
#: the metering summary's `all` window row.
METERING_SUMMARY_TOPIC = "onex.snapshot.projection.metering-summary.v1"
METERING_SAVINGS_FIELD = "savings_usd"
METERING_ALL_WINDOW = "all"
#: The Runs row the ticket names: the receipt's correlation id, with the model and
#: token figures the Runs page renders. The Runs exposure declares no per-run
#: cost column, so cost is not asserted here (C30 grades it from the trace).
RUNS_FIELDS = ("model_name", "tokens_input", "tokens_output")
#: "Equals onex metering --json to the cent": equal once both are rounded to cents.
CENT = Decimal("0.005")


def _grade_runs_row(read: ExposureRead, correlation_id: str) -> Clause:
    """The served Runs exposure holds the receipt's correlation id, with its figures."""
    row = next(
        (r for r in read.rows if r.get("correlation_id") == correlation_id), None
    )
    if row is None:
        return Clause(
            "Runs exposure holds the receipt's correlation id",
            False,
            f"{len(read.rows)} served row(s); none carries correlation_id={correlation_id}",
        )
    missing = [f for f in RUNS_FIELDS if row.get(f) in (None, "")]
    return Clause(
        "Runs exposure holds the receipt's correlation id",
        not missing,
        (
            f"correlation_id={correlation_id}; model_name={row.get('model_name')!r}; "
            f"tokens={row.get('tokens_input')}/{row.get('tokens_output')}"
            if not missing
            else f"row for correlation_id={correlation_id} has no {', '.join(missing)}"
        ),
    )


def _grade_overview_savings(read: ExposureRead, metering: dict[str, Any]) -> Clause:
    """Overview's savings figure (the metering summary's `all` row) equals `onex metering --json`.

    Equal to the cent, with the same baseline model string. A null on either
    side is a failure, never a match: "both unmeasured" is not parity, it is two
    figures that were never compared.
    """
    name = "Overview savings (metering-summary all row) equals onex metering --json to the cent"
    row = next(
        (r for r in read.rows if r.get("window_kind") == METERING_ALL_WINDOW), None
    )
    if row is None:
        kinds = sorted({str(r.get("window_kind")) for r in read.rows})
        return Clause(
            name,
            False,
            f"no window_kind={METERING_ALL_WINDOW!r} row among {len(read.rows)} served (kinds: {kinds})",
        )
    expected = metering.get("savings_usd")
    actual = row.get(METERING_SAVINGS_FIELD)
    if actual is None or expected is None:
        return Clause(
            name,
            False,
            f"dashboard {METERING_SAVINGS_FIELD}={actual!r}; metering savings_usd={expected!r}",
        )
    try:
        residual = (Decimal(str(actual)) - Decimal(str(expected))).copy_abs()
    except (ArithmeticError, ValueError):
        return Clause(
            name, False, f"not decimal: dashboard={actual!r}; metering={expected!r}"
        )
    baseline = metering.get("baseline")
    expected_model = baseline.get("model") if isinstance(baseline, dict) else None
    actual_model = row.get("baseline_model")
    same_model = bool(expected_model) and actual_model == expected_model
    return Clause(
        name,
        residual < CENT and same_model,
        f"dashboard={actual} USD; metering={expected} USD; residual={residual} USD; "
        f"baseline model dashboard={actual_model!r}, metering={expected_model!r}",
    )


def grade_dashboard(
    runs: ExposureRead,
    savings: ExposureRead,
    *,
    correlation_id: str,
    metering: dict[str, Any],
) -> tuple[list[Precondition], list[Clause]]:
    """Each exposure either grades a clause or names the surface still missing."""
    preconditions: list[Precondition] = []
    clauses: list[Clause] = []
    graders: tuple[tuple[ExposureRead, Callable[[], Clause]], ...] = (
        (runs, lambda: _grade_runs_row(runs, correlation_id)),
        (savings, lambda: _grade_overview_savings(savings, metering)),
    )
    for read, grade in graders:
        absent = read.absent()
        if absent is not None:
            preconditions.append(absent)
        else:
            clauses.append(grade())
    return preconditions, clauses


def read_dashboard(
    env: dict[str, str], correlation_id: str
) -> tuple[Precondition, ExposureRead, ExposureRead]:
    """Start the real `onex dashboard` on the scratch HOME and read both exposures."""
    with running_dashboard(env) as start:
        started = Precondition(
            "onex dashboard serves on loopback",
            start.base_url is not None,
            start.detail,
            "OMN-19976",
        )
        if start.base_url is None:
            return (
                started,
                ExposureRead(DECISIONS_TOPIC, None),
                ExposureRead(METERING_SUMMARY_TOPIC, None),
            )
        return (
            started,
            read_exposure(
                start.base_url,
                DECISIONS_TOPIC,
                token=start.token,
                correlation_id=correlation_id,
            ),
            read_exposure(start.base_url, METERING_SUMMARY_TOPIC, token=start.token),
        )


def _grade_metering_contract(payload: dict[str, Any]) -> list[Clause]:
    clauses: list[Clause] = []

    missing = [f for f in REQUIRED_SUMMARY_FIELDS if f not in payload]
    clauses.append(
        Clause(
            name="metering payload carries the fields the pages read",
            ok=not missing,
            detail="all present" if not missing else f"missing: {', '.join(missing)}",
        )
    )

    savings = payload.get("savings_usd")
    measured = payload.get("runs_measured")
    # "None -- never zero -- when the baseline is unresolved or no run was fully
    # measured." A zero here is the defect: it renders as a real figure meaning
    # "we saved nothing" when the truth is "nothing was measured".
    if measured == 0:
        ok = savings is None
        clauses.append(
            Clause(
                name="unmeasured savings is null, never zero",
                ok=ok,
                detail=(
                    "runs_measured is 0 and savings_usd is null"
                    if ok
                    else f"runs_measured is 0 but savings_usd is {savings!r}"
                ),
            )
        )
    else:
        clauses.append(
            Clause(
                name="unmeasured savings is null, never zero",
                ok=True,
                detail=f"not exercised: runs_measured is {measured!r}, so the rule does not bind",
            )
        )

    # "A savings figure is never printed without the counterfactual baseline it
    # was derived against." The demo's hero label said "Verified savings" with
    # nothing naming a baseline; this is the clause that refuses that shape.
    baseline = payload.get("baseline")
    if savings is not None:
        named: dict[str, Any] = baseline if isinstance(baseline, dict) else {}
        have = all(named.get(f) for f in REQUIRED_BASELINE_FIELDS)
        clauses.append(
            Clause(
                name="a savings figure names its baseline and pricing manifest",
                ok=have,
                detail=(
                    f"baseline model {named.get('model')!r}, manifest "
                    f"{named.get('pricing_manifest_version')!r}"
                    if have
                    else f"savings_usd is {savings!r} but baseline is {baseline!r}"
                ),
            )
        )
    else:
        clauses.append(
            Clause(
                name="a savings figure names its baseline and pricing manifest",
                ok=True,
                detail="not exercised: savings_usd is null, so there is no figure to justify",
            )
        )

    total = payload.get("runs_total")
    if isinstance(total, int) and isinstance(measured, int):
        ok = measured <= total
        clauses.append(
            Clause(
                name="measured runs are a subset of total runs",
                ok=ok,
                detail=f"runs_measured {measured} of runs_total {total}",
            )
        )

    # spend and counterfactual must agree with savings to the cent, or the
    # dashboard is reconciling against arithmetic that does not close.
    spend, cf = payload.get("spend_usd"), payload.get("counterfactual_usd")
    if savings is not None and spend is not None and cf is not None:
        try:
            delta = (
                Decimal(str(cf)) - Decimal(str(spend)) - Decimal(str(savings))
            ).copy_abs()
            ok = delta <= Decimal("0.01")
            clauses.append(
                Clause(
                    name="counterfactual minus spend equals savings",
                    ok=ok,
                    detail=f"residual {delta} USD",
                )
            )
        except (ArithmeticError, ValueError) as exc:
            clauses.append(
                Clause(
                    name="counterfactual minus spend equals savings",
                    ok=False,
                    detail=f"figures are not decimal-parseable: {exc}",
                )
            )

    return clauses


def refresh_metering_summary_store(db_path: Path, tenant_id: str) -> Precondition:
    """Refresh the stored metering summary the seeded store's `onex metering` and Overview read.

    omnimarket >= 0.4.301 serves `onex metering` from the stored row, which the
    delegation path refreshes itself. The fixture is seeded by calling the
    projection directly, so nothing has refreshed it. `include_fixtures=True`
    because the fixture row is marked as a fixture and is skipped otherwise.
    """
    name = "metering summary refreshed after seeding"
    try:
        from omnimarket.projection.sqlite_metering_summary import (
            refresh_metering_summary,
            resolve_metering_baseline_model,
        )
    except ImportError as exc:
        return Precondition(
            name, False, f"omnimarket >= 0.4.301 is required: {exc}", "OMN-19984"
        )
    try:
        baseline = resolve_metering_baseline_model().model
        rows = refresh_metering_summary(
            db_path, tenant_id, baseline, datetime.now(UTC), include_fixtures=True
        )
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        return Precondition(
            name, False, f"refresh raised {type(exc).__name__}", "OMN-19984"
        )
    kinds = sorted({r.window_kind for r in rows})
    return Precondition(
        name,
        METERING_ALL_WINDOW in kinds,
        f"{len(rows)} row(s), window kinds {kinds}, baseline {baseline!r}",
        "OMN-19984",
    )


def run(
    *,
    dashboard: Callable[
        [dict[str, str], str], tuple[Precondition, ExposureRead, ExposureRead]
    ]
    | None = None,
    identity: Callable[[dict[str, str]], tuple[Precondition, str | None]] | None = None,
    refresh: Callable[[Path, str], Precondition] | None = None,
) -> ProbeResult:
    """Grade C31. `dashboard`, `identity` and `refresh` are injectable so tests start nothing."""
    dashboard = dashboard if dashboard is not None else read_dashboard
    identity = identity if identity is not None else local_tenant_identity
    refresh = refresh if refresh is not None else refresh_metering_summary_store
    result = ProbeResult(criterion=CRITERION)
    cli_pre = onex_cli()
    result.preconditions.append(cli_pre)

    # One fixture delegation, into a store this probe owns: the default store
    # under a scratch HOME, because `onex dashboard` serves whatever store its
    # HOME resolves. C31's proof text is "a clean container installs, runs one
    # delegation and starts the dashboard"; the identity comes first, since the
    # served exposures are tenant-scoped and metering keys its summary on it.
    payload: dict[str, Any] | None = None
    with tempfile.TemporaryDirectory(
        prefix="c31-probe-", dir=os.environ.get("RUNNER_TEMP") or None
    ) as tmp:
        home = Path(tmp) / "home"
        db_path = home / ".omninode" / "delegation" / "delegation.sqlite"
        home.mkdir(parents=True, exist_ok=True)
        probe_env = os.environ.copy()
        probe_env["HOME"] = str(home)
        tenant: str | None = None
        if cli_pre.present:
            identity_pre, tenant = identity(probe_env)
            result.preconditions.append(identity_pre)
        if tenant is not None:
            seeded = seed_fixture_delegation(db_path, tenant_id=tenant)
            result.preconditions.append(seeded)
            if seeded.present:
                result.preconditions.append(refresh(db_path, tenant))

        metering_pre, payload = onex_metering_json(
            db_path if tenant is not None else None, env=probe_env
        )
        result.preconditions.append(metering_pre)

        dashboard_command = onex_subcommand("dashboard", owner="OMN-19976")
        result.preconditions.append(dashboard_command)

        # Grade every clause whose surface exists, even while another precondition
        # is still missing. This CANNOT publish a green on partial evidence:
        # `verdict()` returns PRECONDITION-ABSENT whenever anything is missing and
        # never looks at clauses to decide it, so the criterion stays red until the
        # conjunction holds. What the clauses buy is a readout in place of one flat
        # red that says only "not everything exists yet".
        if payload is not None:
            result.clauses.extend(_grade_metering_contract(payload))
        if tenant is not None and dashboard_command.present and payload is not None:
            started, runs, savings = dashboard(probe_env, FIXTURE_CORRELATION_ID)
            result.preconditions.append(started)
            if started.present:
                missing, clauses = grade_dashboard(
                    runs,
                    savings,
                    correlation_id=FIXTURE_CORRELATION_ID,
                    metering=payload,
                )
                result.preconditions.extend(missing)
                result.clauses.extend(clauses)
    return result
