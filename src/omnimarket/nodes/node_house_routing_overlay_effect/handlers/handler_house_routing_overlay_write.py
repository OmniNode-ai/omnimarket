# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: declare or retire a HOUSE rung in the routing overlay (OMN-19186).

Operator ruling 2026-09-22, firm: lab configuration lives in contract
overlays, and registering a lab inference rung must not require a pull
request. ``delegation_routing_tenant_overlay`` is the only binding surface in
this system that is a store write with no pull request and no restart. This
node is the writer for the house arm of it.

It is NOT the BYOK credential bridge, deliberately.
``node_projection_tenant_credentials`` writes the customer arm of the same
table from a ``credential-registered`` event, and its entire job is to bind a
customer to THEIR OWN key -- an undeclared provider mints no row precisely so
a customer can never inherit a house credential. Teaching it to mint house
rows would put the two opposite obligations in one consumer, and the first
thing to go wrong would be a customer row minted with house provenance. A
separate node keeps the two arms unable to reach each other: this one cannot
name a tenant at all (``tenant_id`` is a constant here), and that one cannot
name the house.

Fail-closed surfaces, and what each one is protecting against:

* a partial declaration is un-representable (the model, not this handler);
* ``tier_name`` is checked against the DEPLOYED routing-tiers contract, so a
  declaration cannot invent a tier -- v1(a) keeps routing structure
  platform-fixed, and a rung in a tier nothing iterates is a row that never
  routes;
* an unreadable tiers contract REFUSES rather than skipping the check, because
  a validation that silently stops validating is worse than one that is absent;
* ``endpoint_url`` is validated for SHAPE and never probed. A declared rung
  whose host is down is a health fact (AC4). A writer that probed would refuse
  a correct declaration during a reboot and would make a malformed URL and an
  offline box the same error.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import yaml

from omnimarket.nodes.node_house_routing_overlay_effect.models.model_house_routing_overlay import (
    EnumHouseOverlayOperation,
    ModelHouseRoutingOverlayCommand,
    ModelHouseRoutingOverlayDeclaration,
    ModelHouseRoutingOverlayReceipt,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.routing_tiers_path import resolve_routing_tiers_path
from omnimarket.routing.tenant_overlay_resolver import TENANT_OVERLAY_TABLE

logger = logging.getLogger(__name__)

#: The table's UNIQUE (tenant_id, task_type) constraint, in the comma-joined
#: form the projection adapters take.
_CONFLICT_KEY = "tenant_id,task_type"


class HouseOverlayWriteError(RuntimeError):
    """A house-rung write that must not proceed."""


class ProtocolHouseOverlayStore(Protocol):
    """The I/O boundary this EFFECT writes through.

    Structurally satisfied by
    :class:`omnimarket.projection.postgres_sync_database.PostgresSyncProjectionAdapter`
    and by test doubles. Injected rather than constructed so the node is
    provable without a database and so the deployed binding is a deployment
    decision rather than an import.
    """

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = ...,
        returning: tuple[str, ...] = ...,
    ) -> list[dict[str, object]]:
        """Insert or replace one row, returning the stored columns asked for."""
        ...

    def delete(
        self,
        table: str,
        filters: Mapping[str, object],
        *,
        tenant: str | None = ...,
    ) -> int:
        """Delete matching rows, returning how many were removed."""
        ...


def _declared_tier_names(tiers_path: Path) -> frozenset[str]:
    """Read the tier vocabulary from the deployed routing-tiers contract.

    Fail-closed: an unreadable, unparseable or tier-less contract raises. The
    alternative -- skipping the check when the file cannot be read -- would
    make the only failure mode that matters (a contract this deployment does
    not actually have) the one case where nothing is validated.
    """
    try:
        document = yaml.safe_load(tiers_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise HouseOverlayWriteError(
            f"routing tiers contract at {tiers_path} could not be read, so a "
            "declaration's tier membership cannot be validated; refusing the "
            "write rather than storing an unvalidated rung"
        ) from exc
    tiers = (document or {}).get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise HouseOverlayWriteError(
            f"routing tiers contract at {tiers_path} declares no tiers"
        )
    names = {
        str(tier["name"]).strip()
        for tier in tiers
        if isinstance(tier, dict) and str(tier.get("name", "")).strip()
    }
    if not names:
        raise HouseOverlayWriteError(
            f"routing tiers contract at {tiers_path} declares no named tiers"
        )
    return frozenset(names)


class HandlerHouseRoutingOverlayWrite:
    """Declare or retire the house's own routing-overlay rows."""

    handler_type = "node_handler"
    handler_category = "effect"

    def __init__(
        self,
        store: ProtocolHouseOverlayStore | None = None,
        *,
        tier_names: frozenset[str] | None = None,
        tiers_path: Path | None = None,
    ) -> None:
        self._store = store
        self._tier_names = tier_names
        self._tiers_path = tiers_path

    def _resolve_tier_names(self) -> frozenset[str]:
        if self._tier_names is not None:
            return self._tier_names
        return _declared_tier_names(self._tiers_path or resolve_routing_tiers_path())

    def _row_for(
        self, declaration: ModelHouseRoutingOverlayDeclaration, task_type: str
    ) -> dict[str, object]:
        return {
            # The constant IS the isolation control on this path. There is no
            # caller-supplied tenant anywhere in this node.
            "tenant_id": HOUSE_TENANT_SLUG,
            "task_type": task_type,
            "backend_id": declaration.backend_id,
            "provider": declaration.provider,
            "endpoint_url": declaration.endpoint_url,
            "model_name": declaration.model_name,
            "secret_ref": declaration.secret_ref,
            "timeout_ms": declaration.timeout_ms,
            "max_tokens": declaration.max_tokens,
        }

    async def handle(
        self, request: ModelHouseRoutingOverlayCommand
    ) -> ModelHouseRoutingOverlayReceipt:
        if self._store is None:
            raise HouseOverlayWriteError(
                "no overlay store injected; a house-rung write with nowhere to "
                "go must refuse rather than report a registration that does not "
                "exist"
            )
        if request.operation is EnumHouseOverlayOperation.DECLARE:
            return self._declare(request)
        return self._retire(request)

    def _declare(
        self, request: ModelHouseRoutingOverlayCommand
    ) -> ModelHouseRoutingOverlayReceipt:
        declaration = request.declaration
        assert declaration is not None  # narrowed by the command's validator
        tier_names = self._resolve_tier_names()
        if declaration.tier_name not in tier_names:
            raise HouseOverlayWriteError(
                f"tier {declaration.tier_name!r} is not declared by the routing "
                f"tiers contract (declared: {sorted(tier_names)}). v1(a) keeps "
                "routing structure platform-fixed, so a rung may join an "
                "existing tier but may not introduce one."
            )
        assert self._store is not None
        rows_written = 0
        for task_type in declaration.task_types:
            stored = self._store.upsert_returning(
                TENANT_OVERLAY_TABLE,
                _CONFLICT_KEY,
                self._row_for(declaration, task_type),
                tenant=HOUSE_TENANT_SLUG,
                returning=("tenant_id", "task_type", "backend_id"),
            )
            rows_written += len(stored)
        logger.info(
            "house rung declared: backend_id=%s tier=%s task_types=%s rows=%d "
            "endpoint=%s secret_ref_declared=%s",
            declaration.backend_id,
            declaration.tier_name,
            list(declaration.task_types),
            rows_written,
            declaration.endpoint_url,
            declaration.secret_ref is not None,
        )
        return ModelHouseRoutingOverlayReceipt(
            operation=EnumHouseOverlayOperation.DECLARE,
            tenant_id=HOUSE_TENANT_SLUG,
            backend_id=declaration.backend_id,
            tier_name=declaration.tier_name,
            task_types=declaration.task_types,
            rows_written=rows_written,
            endpoint_url=declaration.endpoint_url,
            model_name=declaration.model_name,
            secret_ref_declared=declaration.secret_ref is not None,
            endpoint_reachability_probed=False,
        )

    def _retire(
        self, request: ModelHouseRoutingOverlayCommand
    ) -> ModelHouseRoutingOverlayReceipt:
        backend_id = (request.retire_backend_id or "").strip()
        assert self._store is not None
        rows_removed = 0
        for task_type in request.retire_task_types:
            # backend_id is in the filter so a retire cannot remove a DIFFERENT
            # rung that happens to serve the same task type -- the caller says
            # what they are withdrawing and the statement holds them to it.
            rows_removed += self._store.delete(
                TENANT_OVERLAY_TABLE,
                {
                    "tenant_id": HOUSE_TENANT_SLUG,
                    "task_type": task_type,
                    "backend_id": backend_id,
                },
                tenant=HOUSE_TENANT_SLUG,
            )
        logger.info(
            "house rung retired: backend_id=%s task_types=%s rows_removed=%d",
            backend_id,
            list(request.retire_task_types),
            rows_removed,
        )
        return ModelHouseRoutingOverlayReceipt(
            operation=EnumHouseOverlayOperation.RETIRE,
            tenant_id=HOUSE_TENANT_SLUG,
            backend_id=backend_id,
            task_types=request.retire_task_types,
            rows_removed=rows_removed,
            endpoint_reachability_probed=False,
        )


__all__ = [
    "HandlerHouseRoutingOverlayWrite",
    "HouseOverlayWriteError",
    "ProtocolHouseOverlayStore",
]
