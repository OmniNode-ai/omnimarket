# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18159: the two delegation-savings views read as their invoker.

WHY THIS EXISTS

``node_projection_delegation``'s migration 0040 gave the four delegation
aggregate views invoker rights so a read evaluates row-level security against
the CALLER rather than the view owner. A live read-only readback of onex-dev on
2026-09-11 then found two more views selecting from ``delegation_events`` with
``security_invoker`` unset -- ``projection_delegation_savings`` and
``projection_delegation_savings_series``. They were missed because they belong
to a different node with a different migration lineage, not because anything
about them is different. Operator ruling 2026-09-11T14:40:49Z corrected the
exposure set from four to six.

WHERE THE BEFORE/AFTER PROOF LIVES

Not here. These migrations are schema-qualified to ``public``, so they cannot be
isolated into a disposable schema the way ``node_projection_delegation``'s can:
running them against any live database REPLACES that database's public views.
The before/after pair is therefore proven in omnibase_infra against a throwaway
cluster, alongside the vendored copy of the migration, where ``public`` is
disposable.

The behavioural claim -- that invoker rights make the tenant policy on
``delegation_events`` evaluate against the caller -- is 0040's, proven there for
the four sibling views over the same base table under the same FORCE row-level
security. This migration issues the same statement against the same table
through two more views; re-deriving Postgres' documented semantics here would
test Postgres, not the change.

WHY THE EXPOSURES WERE NOT DECLARED TENANT-SCOPED HERE, AND WHERE THAT WENT

``ProjectionTableConfig`` hard-fails contract load for a ``tenant_column`` on an
exposure that is not ``bus_backed``, so when this module landed neither savings
exposure could carry one, and this file asserted their absence rather than
describing it.

OMN-17426 converted ``projection_delegation_savings``: it is bus-backed,
grouped on ``tenant_id`` by migration 089, and declares the ``tenant_column``
088's header said would land "when the exposures convert". So the assertion
below now reads the landed state for that view and the still-absent state for
``projection_delegation_savings_series``, which has no publish site and stays
SQL-served. The constructed-model control is unchanged: it is about the
validator, not about either contract, and it is what keeps the refusal this
module was written around a passing test rather than a claim in a comment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SAVINGS_VIEWS = (
    "projection_delegation_savings",
    "projection_delegation_savings_series",
)


@pytest.mark.unit
def test_a_tenant_column_lands_exactly_when_its_exposure_converts() -> None:
    """The rule this module was written around, now observed in both states.

    A ``tenant_column`` on an exposure that is not ``bus_backed`` hard-fails
    contract load, so both halves land together or neither does. As of
    OMN-17426 one of these two views has converted and the other has not, which
    makes that rule checkable rather than merely stated.
    """
    import yaml
    from pydantic import ValidationError

    from omnimarket.projection.models import ProjectionTableConfig

    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_projection_savings/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    exposures = {
        exposure["table"]: exposure
        for exposure in contract["projection_api"]["exposures"]
        if exposure.get("table") in SAVINGS_VIEWS
    }
    assert set(exposures) == set(SAVINGS_VIEWS), "positive control: both are declared"
    # Converted by OMN-17426: bus-backed, so the tenant_column is both legal
    # and required -- the writer publishes per tenant and this is what makes
    # the serving path read per tenant.
    converted = exposures["projection_delegation_savings"]
    assert converted.get("bus_backed") is True
    assert converted.get("tenant_column") == "tenant_id"
    # Unconverted: no publish site, so no flag, so no tenant_column either.
    series = exposures["projection_delegation_savings_series"]
    assert not series.get("bus_backed", False)
    assert series.get("tenant_column") is None

    with pytest.raises(ValidationError, match="is not bus_backed"):
        ProjectionTableConfig(
            topic="onex.snapshot.projection.delegation.savings.v1",
            table="projection_delegation_savings",
            schema_name="public",
            columns=("tenant_id", "session_count"),
            limit=1,
            bus_backed=False,
            tenant_column="tenant_id",
        )

    # The control: the SAME declaration is accepted once the exposure is
    # bus-backed, so the refusal above is about bus-backing and not about the
    # column being unservable in principle.
    accepted = ProjectionTableConfig(
        topic="onex.snapshot.projection.delegation.savings.v1",
        table="projection_delegation_savings",
        schema_name="public",
        columns=("tenant_id", "session_count"),
        limit=1,
        bus_backed=True,
        key_columns=("tenant_id",),
        tenant_column="tenant_id",
    )
    assert accepted.tenant_scoped is True
