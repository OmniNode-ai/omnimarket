# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Terminal statuses for a DoD verification run.

Lives in the shared enums package, not inside ``node_dod_verify``, because
``node_dod_sweep_orchestrator`` reconciles against the same taxonomy
(OMN-17022) and a sibling node may never reach into another node's models
package (OMN-9263). ``EnumCheckProofClass`` already sits here for the same
reason and is re-exported from ``model_dod_verify_state`` the same way.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodVerifyStatus(StrEnum):
    """Status values for DoD verification."""

    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"
    # OMN-17022 (off-rails A15): the run reached NO verdict — it faulted, was
    # killed by a caller-side timeout, or could not resolve the binding it
    # needed to look at anything. Before this member existed, such a run was
    # indistinguishable from PENDING (the field's own default), which reads as
    # "not yet attempted" — which is precisely why the ten items held by the
    # 2026-08-29 sprint-triage closeout were never re-run without re-running
    # the whole audit. UNRESOLVED is terminal for the run that produced it and
    # never counts toward completion; a retry, when the cause allows one, is a
    # NEW attempt recorded alongside it, not a mutation of it.
    UNRESOLVED = "unresolved"


__all__: list[str] = ["EnumDodVerifyStatus"]
