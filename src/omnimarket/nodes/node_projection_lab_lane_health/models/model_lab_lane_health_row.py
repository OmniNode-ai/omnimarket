"""The materialized lab lane-health row (OMN-18769).

One row per lab lane. Three contributing facts, each carrying **its own**
timestamp, its own pre-decay verdict and its own decayed verdict:

* ``census`` is produced by the ``omnibase_infra`` lane-census refresh, on
  every run, and is hours to days old.
* ``health`` is produced by the runtime's own health monitor, and is seconds
  to minutes old.
* ``receipt`` is produced by the lab-pass receipt emitter under ``always()``,
  so a FAIL is recorded too, and is per merged sha.

**Why not one ``updated_at``.** These ages differ by four orders of magnitude.
A single row-level freshness column lets the health fact -- which is seconds old
by construction -- make a two-day-old census look current. That is not a
hypothetical: it is the archived topology view's actual failure mode, and it is
why the ticket's AC5 falsifier is a reducer test feeding a fresh health fact
beside a 30-hour-old census fact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_lab_lane_health.models.enum_fact_status import (
    EnumFactStatus,
    decay,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.enum_lab_lane import (
    EnumLabLane,
)


class ModelLaneCensusFact(BaseModel):
    """What the lane census last said about this lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    #: The number of drift items the census recorded FOR THIS LANE. Not the
    #: run-wide total: a run covers several lanes and only this lane's items
    #: belong on this lane's row.
    drift_count: int = Field(ge=0)
    #: The drift items themselves, verbatim from the census payload, so a panel
    #: can name what is missing or extra rather than render a bare count.
    drift_items: tuple[dict[str, str], ...] = ()
    #: The census host, so a reader can tell which machine was looked at.
    host: str = ""

    @property
    def status(self) -> EnumFactStatus:
        """PASS when the lane matches its declared census, FAIL when it drifts.

        Severity is not consulted. The census's own severity field grades an
        alert's urgency; this field answers "does the lane match what is
        declared", and any drift at all is a no.
        """
        return EnumFactStatus.PASS if self.drift_count == 0 else EnumFactStatus.FAIL


class ModelLaneHealthFact(BaseModel):
    """The runtime's own per-dimension health verdict for this lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    #: HEALTHY / DEGRADED / CRITICAL as the runtime monitor states it.
    aggregate: str
    #: ``[{"name": ..., "status": ..., "detail": ...}, ...]`` verbatim. Carried
    #: whole rather than summarized so a panel can list the failing dimension
    #: by name instead of showing an aggregate nobody can act on.
    dimensions: tuple[dict[str, str], ...] = ()

    @property
    def status(self) -> EnumFactStatus:
        """Map the runtime's three-value aggregate onto the fact vocabulary."""
        normalized = self.aggregate.strip().upper()
        if normalized == "HEALTHY":
            return EnumFactStatus.PASS
        if normalized == "DEGRADED":
            return EnumFactStatus.WARN
        if normalized == "CRITICAL":
            return EnumFactStatus.FAIL
        # A verdict this reducer does not recognise is UNKNOWN, never PASS.
        # A renamed enum member upstream must surface as "we cannot read this",
        # which is a bug report; silently grading it green is an outage.
        return EnumFactStatus.UNKNOWN


class ModelLaneReceiptFact(BaseModel):
    """The most recent lab-pass receipt for this lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    sha: str
    #: ``PASS`` or ``FAIL`` as the receipt's own derived verdict states it. The
    #: receipt model derives it from the check set, so an emitter cannot record
    #: green over a failed readback; this reducer does not re-derive it.
    result: str
    #: Names of the checks that did not pass. Empty on a PASS receipt.
    failing_checks: tuple[str, ...] = ()

    @property
    def status(self) -> EnumFactStatus:
        normalized = self.result.strip().upper()
        if normalized == "PASS":
            return EnumFactStatus.PASS
        if normalized == "FAIL":
            return EnumFactStatus.FAIL
        return EnumFactStatus.UNKNOWN


class ModelLabLaneHealthRow(BaseModel):
    """One lane's health, as the exposure serves it.

    Every fact is optional. A lane with no receipt yet is a real state and its
    receipt dimension reports UNKNOWN -- never PASS, and never an invented
    zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: EnumLabLane
    census: ModelLaneCensusFact | None = None
    health: ModelLaneHealthFact | None = None
    receipt: ModelLaneReceiptFact | None = None

    def to_exposure_row(self, *, now: datetime) -> dict[str, Any]:
        """Render the wire row the exposure publishes.

        ``now`` is a parameter, not a clock read, because the decay is part of
        the row's content: two consumers evaluating the same row must reach the
        same verdict, and a test must be able to state an age.

        Each dimension contributes FOUR columns -- observed-at, pre-decay
        verdict, decayed verdict, and its payload -- because collapsing any of
        the four loses a distinction a reader needs. Dropping observed-at loses
        the age; dropping the pre-decay verdict turns "was PASS, now stale" into
        "STALE, cause unknown"; dropping the decayed verdict pushes the decay
        into every widget, where they will each implement it differently.
        """
        census = self.census
        health = self.health
        receipt = self.receipt
        return {
            "lane": self.lane.value,
            "lane_class": "lab",
            # --- census -------------------------------------------------
            "census_observed_at": _iso(census.observed_at if census else None),
            "census_original_status": _original(census),
            "census_status": _decayed(census, now=now),
            "census_drift_count": census.drift_count if census else None,
            "census_drift_items": [dict(item) for item in census.drift_items]
            if census
            else [],
            "census_host": census.host if census else "",
            # --- runtime health -----------------------------------------
            "health_observed_at": _iso(health.observed_at if health else None),
            "health_original_status": _original(health),
            "health_status": _decayed(health, now=now),
            "health_aggregate": health.aggregate if health else "",
            "health_dimensions": [dict(d) for d in health.dimensions] if health else [],
            # --- lab-pass receipt ---------------------------------------
            "receipt_observed_at": _iso(receipt.observed_at if receipt else None),
            "receipt_original_status": _original(receipt),
            "receipt_status": _decayed(receipt, now=now),
            "receipt_sha": receipt.sha if receipt else "",
            "receipt_result": receipt.result if receipt else "",
            "receipt_failing_checks": list(receipt.failing_checks) if receipt else [],
            # --- when this row was rendered -----------------------------
            # Deliberately NOT a freshness column for any fact above. It says
            # when the reducer last folded an event into this lane, and it is
            # named so that nobody mistakes it for the age of the data.
            "projected_at": now.isoformat(),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _original(
    fact: ModelLaneCensusFact | ModelLaneHealthFact | ModelLaneReceiptFact | None,
) -> str:
    return (fact.status if fact is not None else EnumFactStatus.UNKNOWN).value


def _decayed(
    fact: ModelLaneCensusFact | ModelLaneHealthFact | ModelLaneReceiptFact | None,
    *,
    now: datetime,
) -> str:
    if fact is None:
        return EnumFactStatus.UNKNOWN.value
    return decay(fact.status, fact.observed_at, now=now).value
