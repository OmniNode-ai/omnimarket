# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation routing starts at the cheapest tier measured good enough (OMN-20170).

Plan task RS.6. INV-063 tries the cheapest configured responder first. With the
capability map (RS.5, ``delegation_capability_cells``) and the request's
measured size band (RS.3), cheapest-first becomes cheapest-good-enough-first:
for a request of class C and band B the chain starts at the cheapest tier in the
static tier order that serves a backend whose cell for (C, B) is MET, and
escalates from there in the usual order.

Three fallbacks keep the static order: a band with no MET cell, an unreadable
map, and a class whose eval false-pass line is not MET (EV.9, OMN-19797), since
a class whose gate is not trusted learns nothing. A request with no measured
band also keeps the static order.

``resolve_capability_start`` is pure: the cells and the eval line are read at
the caller's I/O boundary, like the ROI overlay, so the routing reducer stays a
function of its inputs. The decision is logged with the class, band, chosen
cell, its n and its bound.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, Protocol

from omnimarket.adapters.asyncpg_adapter import DB_URL_ENV, AsyncpgAdapter
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)
from omnimarket.models.delegation_capability_map.model_capability_map_read import (
    ModelCapabilityMapRead,
)
from omnimarket.models.delegation_capability_map.model_capability_start import (
    ModelCapabilityStart,
)
from omnimarket.projection.tenant_isolation import TENANT_GUC

_logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 3
DELEGATION_CAPABILITY_CELLS_RELATION = "public.delegation_capability_cells"
_RELATION_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

#: The reason the start tier is what it is.
REASON_BAND_CHEAPEST_MET = "band_cheapest_met"
REASON_NO_MET_CELL = "band_no_met_static"
REASON_MAP_UNREADABLE = "map_unreadable_static"
REASON_CLASS_LINE_NOT_MET = "class_line_not_met_static"
REASON_BAND_UNMEASURED = "band_unmeasured_static"

_EVAL_LINE_MET = "met"


class ProtocolRoutingTier(Protocol):
    """The part of a routing tier the start read needs."""

    @property
    def name(self) -> str: ...

    @property
    def models(self) -> Sequence[ProtocolRoutingTierModel]: ...


class ProtocolRoutingTierModel(Protocol):
    @property
    def backend_ref(self) -> str: ...


def resolve_capability_start(
    tiers: Sequence[ProtocolRoutingTier],
    cells: Sequence[ModelCapabilityCell] | None,
    *,
    task_class: str,
    size_band: EnumSizeBand | None,
    eval_line_verdict: str | None,
) -> ModelCapabilityStart:
    """The cheapest tier in ``tiers`` order with a MET cell for the class and band.

    ``cells`` is None when the map could not be read. The chosen cell is the MET
    cell of that tier with the highest decision lower bound, ties broken by
    backend and configuration id, so the read is deterministic.
    """

    def static(reason: str) -> ModelCapabilityStart:
        return ModelCapabilityStart(
            task_class=task_class,
            size_band=size_band,
            start_tier_name=None,
            reason=reason,
        )

    if cells is None:
        return static(REASON_MAP_UNREADABLE)
    if (eval_line_verdict or "").strip().lower() != _EVAL_LINE_MET:
        return static(REASON_CLASS_LINE_NOT_MET)
    if size_band is None:
        return static(REASON_BAND_UNMEASURED)
    met = [
        cell
        for cell in cells
        if cell.task_class == task_class
        and cell.size_band is size_band
        and cell.verdict is EnumCapabilityCellVerdict.MET
    ]
    for tier in tiers:
        backends = {model.backend_ref for model in tier.models}
        candidates = [cell for cell in met if cell.backend_id in backends]
        if candidates:
            chosen = sorted(
                candidates,
                key=lambda c: (
                    -c.decision_lower_bound,
                    c.backend_id,
                    c.configuration_id,
                ),
            )[0]
            return ModelCapabilityStart(
                task_class=task_class,
                size_band=size_band,
                start_tier_name=tier.name,
                reason=REASON_BAND_CHEAPEST_MET,
                cell=chosen,
            )
    return static(REASON_NO_MET_CELL)


class ProtocolCapabilityCellReader(Protocol):
    """Reads one tenant's latest map run cells for one task class."""

    def read_cells(
        self, *, task_class: str, tenant_id: str
    ) -> tuple[ModelCapabilityCell, ...]:
        """The cells, empty when the class has none; raises when unreadable."""
        ...


class PostgresCapabilityCellReader:
    """Read-only reader of ``delegation_capability_cells`` (psycopg2).

    Reads the cells of the tenant's newest map run for the class, with
    ``app.tenant_id`` set for the table's row-level security and the tenant
    filtered explicitly.
    """

    _COLUMNS = (
        "tenant_id, map_run_id, model_key, backend_id, configuration_id, "
        "task_class, size_band, configuration_parameters, n, passes, pass_rate, "
        "wilson_low, wilson_high, decision_lower_bound, floor, required_n, verdict"
    )

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: int = _CONNECT_TIMEOUT_SECONDS,
        relation: str = DELEGATION_CAPABILITY_CELLS_RELATION,
    ) -> None:
        if not dsn:
            raise ValueError("PostgresCapabilityCellReader requires a non-empty DSN")
        if not _RELATION_PATTERN.match(relation):
            raise ValueError(f"unsafe relation identifier: {relation!r}")
        self._dsn = dsn
        self._connect_timeout = connect_timeout
        self._sql = (
            f"SELECT {self._COLUMNS} FROM {relation} "
            "WHERE tenant_id = %(tenant_id)s::uuid AND task_class = %(task_class)s "
            "AND map_run_id = ("
            f"SELECT map_run_id FROM {relation} "
            "WHERE tenant_id = %(tenant_id)s::uuid "
            "ORDER BY observed_at DESC, map_run_id DESC LIMIT 1)"
        )
        # A psycopg2 connection; psycopg2 ships no type stubs.
        self._conn: Any = None

    def _get_conn(self) -> Any:
        if self._conn is None or self._conn.closed:
            from omnimarket.projection.postgres_read_database import (
                connect_read_only,
            )

            self._conn = connect_read_only(
                self._dsn, connect_timeout=self._connect_timeout
            )
        return self._conn

    def read_cells(
        self, *, task_class: str, tenant_id: str
    ) -> tuple[ModelCapabilityCell, ...]:
        conn = self._get_conn()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, tenant_id))
                cur.execute(
                    self._sql, {"tenant_id": tenant_id, "task_class": task_class}
                )
                rows = cur.fetchall()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
        names = [c.strip() for c in self._COLUMNS.split(",")]
        cells = []
        for row in rows:
            record = dict(zip(names, row, strict=True))
            parameters = record["configuration_parameters"]
            record["configuration_parameters"] = (
                ModelAttemptConfiguration.model_validate_json(parameters)
                if isinstance(parameters, str)
                else ModelAttemptConfiguration.model_validate(parameters)
            )
            cells.append(ModelCapabilityCell.model_validate(record))
        return tuple(cells)


def resolve_capability_cell_reader() -> PostgresCapabilityCellReader | None:
    """The live map reader, gated on the projection DSN; None keeps the static order."""
    # The projection DSN the asyncpg adapter resolves for every projection read.
    dsn = AsyncpgAdapter().dsn.strip()
    if not dsn:
        return None
    try:
        return PostgresCapabilityCellReader(dsn)
    except Exception:
        _logger.warning(
            "resolve_capability_cell_reader failed to construct a reader from %s; "
            "routing keeps the static tier order",
            DB_URL_ENV,
            exc_info=True,
        )
        return None


def read_capability_cells(
    reader: ProtocolCapabilityCellReader | None, *, task_class: str, tenant_id: str
) -> tuple[ModelCapabilityCell, ...] | None:
    """The class's cells, or None when there is no reader or the read fails."""
    if reader is None:
        return None
    try:
        return reader.read_cells(task_class=task_class, tenant_id=tenant_id)
    except Exception:
        _logger.warning(
            "capability map unreadable for task_class=%s; routing keeps the "
            "static tier order",
            task_class,
            exc_info=True,
        )
        return None


__all__ = [
    "DELEGATION_CAPABILITY_CELLS_RELATION",
    "REASON_BAND_CHEAPEST_MET",
    "REASON_BAND_UNMEASURED",
    "REASON_CLASS_LINE_NOT_MET",
    "REASON_MAP_UNREADABLE",
    "REASON_NO_MET_CELL",
    "ModelCapabilityMapRead",
    "ModelCapabilityStart",
    "PostgresCapabilityCellReader",
    "ProtocolCapabilityCellReader",
    "read_capability_cells",
    "resolve_capability_cell_reader",
    "resolve_capability_start",
]
