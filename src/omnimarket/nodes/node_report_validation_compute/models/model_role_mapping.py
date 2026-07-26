# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 7-dispatch-role -> 4-report-role mapping table (OMN-15163).

``omnibase_core.models.dispatch.report.ROLE_TO_MODEL`` (OMN-15161) closes
over exactly 4 report shapes: ``implementer``, ``verifier``, ``lander``,
``scout``. ``node_dispatch_worker`` (OMN-13551 et seq.) dispatches 7 distinct
role-prompt bodies: ``watcher``, ``fixer``, ``designer``, ``auditor``,
``synthesizer``, ``sweep``, ``ops``. This module is the single tested
constant that reconciles the two vocabularies, read by the handler to decide
which of the 4 closed report models a given dispatch worker's raw report
payload must be validated against.

Mapping rationale (read alongside each role's prompt body in
``omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker``):

- ``fixer`` -> ``IMPLEMENTER``. The only dispatch_worker role that writes
  code and opens/updates a PR ("Create worktree ... Push branch, open PR,
  enable auto-merge"), matching ``ModelDispatchReportImplementer`` exactly
  (``pr_number``, ``branch``, ``head_sha``, ``files_changed_paths``).
- ``watcher``, ``designer``, ``auditor``, ``synthesizer``, ``sweep`` ->
  ``SCOUT`` (read-only default, per ticket brief). None of these five make
  code changes or land/merge a PR; each investigates/monitors/produces a
  findings-shaped artifact (a CI-state read for watcher, a design+plan doc
  for designer, a findings doc for auditor, a reconciliation doc for
  synthesizer, a one-line metric for sweep) with no PR of its own required —
  the exact shape ``ModelDispatchReportScout`` (``verdict`` FOUND/NOT_FOUND/
  BLOCKED, ``findings_paths``, optional ``pr_number``) covers generically.

Deliberately UNMAPPED (out of scope, per ticket brief — declared here, not
silently defaulted):

- ``ops``. Unlike the other 6 roles, ``ops`` is not a single bounded task
  with one terminal report: it is a long-running (cap 480 min), DM-driven
  admin session that executes an open-ended sequence of ``gh`` actions
  (ready/merge/comment/edit/checks/view/list/close/reopen) across possibly
  many PRs, replying one line per action, and does not terminate until a
  ``shutdown_request`` arrives. None of the 4 closed report contracts fit:
  ``LANDER`` is the closest by vocabulary ("merges/finalizes a PR") but
  requires exactly one ``pr_number`` and a single MERGED/BLOCKED/ABORTED
  verdict per report — a shape built for one landing attempt, not an
  open-ended multi-PR admin session with no PR of its own. Forcing ``ops``
  into ``LANDER`` would fabricate a single-PR narrative onto a role that may
  legitimately complete having never merged anything. A dispatch_worker
  report tagged ``role=ops`` reaching this validator therefore returns
  ``EnumReportValidationVerdict.INVALID_SHAPE`` with a specific
  "role has no mapped report_role (declared out-of-scope)" violation —
  fail-closed, not silently coerced to ``SCOUT``.

Also note: no dispatch_worker role maps to ``VERIFIER``. Independent
verification of an implementer's claim is a distinct dispatch pattern
(paired worker/verifier dispatch, see the ``onex:verified_dispatch`` skill)
that issues ``role=verifier`` reports directly outside ``node_dispatch_worker``
entirely — it was never one of the 7 dispatch_worker role-prompt bodies, so
there is nothing to map it FROM here.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from omnibase_core.enums.enum_dispatch_report_role import EnumDispatchReportRole

from omnimarket.nodes.node_report_validation_compute.models.model_dispatch_worker_role import (
    EnumDispatchWorkerRole,
)

# MappingProxyType (not a plain dict): the table is a closed routing decision
# baked in at import time -- a mutable public dict would let any importer
# silently alter validation routing (e.g. re-point `ops`), defeating the
# deterministic fail-closed policy this module documents above.
ROLE_MAPPING_TABLE: Final[Mapping[EnumDispatchWorkerRole, EnumDispatchReportRole]] = (
    MappingProxyType(
        {
            EnumDispatchWorkerRole.fixer: EnumDispatchReportRole.IMPLEMENTER,
            EnumDispatchWorkerRole.watcher: EnumDispatchReportRole.SCOUT,
            EnumDispatchWorkerRole.designer: EnumDispatchReportRole.SCOUT,
            EnumDispatchWorkerRole.auditor: EnumDispatchReportRole.SCOUT,
            EnumDispatchWorkerRole.synthesizer: EnumDispatchReportRole.SCOUT,
            EnumDispatchWorkerRole.sweep: EnumDispatchReportRole.SCOUT,
        }
    )
)

# Declared out-of-scope, not silently defaulted. See module docstring.
UNMAPPABLE_DISPATCH_ROLES: frozenset[EnumDispatchWorkerRole] = frozenset(
    {EnumDispatchWorkerRole.ops}
)


def resolve_report_role(
    dispatch_role: EnumDispatchWorkerRole,
) -> EnumDispatchReportRole | None:
    """Return the mapped report role, or ``None`` if ``dispatch_role`` is unmapped.

    ``None`` covers both an explicitly declared-out-of-scope role
    (``UNMAPPABLE_DISPATCH_ROLES``) and — defensively, should the two local
    enums ever drift — any future dispatch role absent from both sets. Either
    way the caller (the handler) must treat ``None`` as a fail-closed
    ``INVALID_SHAPE``, never a silent pass.
    """
    return ROLE_MAPPING_TABLE.get(dispatch_role)


__all__ = [
    "ROLE_MAPPING_TABLE",
    "UNMAPPABLE_DISPATCH_ROLES",
    "resolve_report_role",
]
