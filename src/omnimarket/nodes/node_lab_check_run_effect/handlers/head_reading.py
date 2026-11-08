# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a head's check runs of one name read as, for one current attempt (OMN-22950, J8).

Pure: no clock and no I/O. The same reading as the landing decision's ``read_lab_check`` (parity is pinned by the join
test): only a run on the exact head that carries the current attempt's identity counts. A head with only runs of other
attempts reads ``superseded``; a head with none reads ``pending`` inside the wait bound and ``missing`` past it. A
missing check never reads as a pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    ModelCheckRunRecord,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckVerdict,
)

COMPLETED = "completed"
SUCCESS = "success"


def read_head(
    runs: Sequence[ModelCheckRunRecord],
    *,
    check_name: str,
    head_sha: str,
    current_attempt_id: str,
    dispatched_at: datetime,
    now: datetime,
    wait_bound_seconds: int,
) -> tuple[EnumLabCheckVerdict, str]:
    on_head = [r for r in runs if r.name == check_name and r.head_sha == head_sha]
    current = [r for r in on_head if r.attempt_id == current_attempt_id]
    if any(r.status == COMPLETED and r.conclusion == SUCCESS for r in current):
        return (
            EnumLabCheckVerdict.PASS,
            f"{check_name} passed for attempt {current_attempt_id}",
        )
    if any(r.status != COMPLETED for r in current):
        return (
            EnumLabCheckVerdict.PENDING,
            f"{check_name} is running for attempt {current_attempt_id}",
        )
    if current:
        return (
            EnumLabCheckVerdict.FAILED,
            f"{check_name} did not pass for attempt {current_attempt_id}",
        )
    if on_head:
        others = ", ".join(sorted({r.attempt_id or "unidentified" for r in on_head}))
        return (
            EnumLabCheckVerdict.SUPERSEDED,
            f"{check_name} on the head is from attempt {others}, not the current attempt {current_attempt_id}",
        )
    if now - dispatched_at <= timedelta(seconds=wait_bound_seconds):
        return (
            EnumLabCheckVerdict.PENDING,
            f"{check_name} not yet posted, inside the {wait_bound_seconds} s wait bound",
        )
    return EnumLabCheckVerdict.MISSING, f"no {check_name} run on the head"


__all__ = ["COMPLETED", "SUCCESS", "read_head"]
