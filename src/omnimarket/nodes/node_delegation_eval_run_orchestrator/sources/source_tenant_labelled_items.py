# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-scoped reader of one rater's labelled items of a manifest (OMN-20577).

Implements ``ProtocolDelegationEvalLabelledItems`` over ``delegation_eval_items``. The
reader it is built on is bound to one tenant and sets ``app.tenant_id`` before the
statement, so the relation's RLS policy limits the read to that tenant; the statement
repeats the tenant as an explicit filter so a policy gap cannot widen it.
"""

from __future__ import annotations

from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.events.delegation_gate_eval.enum_gate_verdict import EnumGateVerdict
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.handlers.delegation_eval_read_binding import build_tenant_row_fetch
from omnimarket.nodes.node_delegation_eval_run_orchestrator.protocols.protocol_delegation_eval_labelled_items import (
    ProtocolDelegationEvalLabelledItems,
)
from omnimarket.protocols.protocol_tenant_row_fetch import ProtocolTenantRowFetch

_TABLE = "delegation_eval_items"
_LABELLED_ITEMS_SQL = """
    SELECT item_key, task_class, stratum, label, prompt_snapshot,
           response_snapshot, gate_verdict, deciding_check
    FROM public.delegation_eval_items
    WHERE tenant_id = %s::uuid AND rater_role = %s AND rubric_version = %s
      AND computed_facts ->> 'manifest_id' = %s
    ORDER BY item_key
"""
_VERDICTS = {"accepted": EnumGateVerdict.ACCEPTED, "refused": EnumGateVerdict.REFUSED}


class TenantLabelledItems:
    """Labelled items of the tenant this source was built for, and of no other."""

    def __init__(self, tenant_id: str, reader: ProtocolTenantRowFetch) -> None:
        self._tenant_id = tenant_id
        self._reader = reader

    def get_labelled_items(
        self, manifest_id: str, rater_role: str, rubric_version: str
    ) -> tuple[ModelGateEvalItem, ...]:
        rows = self._reader.fetch(
            _TABLE,
            _LABELLED_ITEMS_SQL,
            (self._tenant_id, rater_role, rubric_version, manifest_id),
        )
        return tuple(
            ModelGateEvalItem(
                item_id=str(row["item_key"]),
                task_class=str(row["task_class"] or ""),
                stratum=str(row["stratum"] or ""),
                label=EnumGateEvalLabel(str(row["label"])),
                prompt_text=str(row["prompt_snapshot"] or ""),
                recorded_answer=(
                    None
                    if row["response_snapshot"] is None
                    else str(row["response_snapshot"])
                ),
                recorded_verdict=_VERDICTS.get(str(row["gate_verdict"] or "")),
                recorded_deciding_check=(
                    str(row["deciding_check"]) if row["deciding_check"] else None
                ),
            )
            for row in rows
        )


def labelled_items_for_tenant(tenant_id: str) -> ProtocolDelegationEvalLabelledItems:
    """The runtime's source for one command: the overlay-bound reader of ``tenant_id``."""
    return TenantLabelledItems(tenant_id, build_tenant_row_fetch(tenant_id))
