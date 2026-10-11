# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which deadlines of the lab job state table have elapsed for a job.

A comparison of the job row's own timestamps against the sweep's ``now``. The
durations come from the contract's ``config.lab_job_check_effect``; the
reducer decides what an elapsed deadline means.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.enums.enum_lab_job import EnumLabJobState
from omnimarket.enums.enum_lab_job_check import EnumLabJobDeadline
from omnimarket.models.lab_job import ModelLabJobRow
from omnimarket.nodes.node_lab_job_check_effect.models import ModelLabJobCheckConfig

_S = EnumLabJobState


def elapsed_deadlines(
    row: ModelLabJobRow, now: datetime, config: ModelLabJobCheckConfig
) -> tuple[EnumLabJobDeadline, ...]:
    """The deadlines of *row*'s current state that have elapsed at *now*."""
    in_state = now - row.entered_state_at
    due: list[EnumLabJobDeadline] = []
    if row.state is _S.QUEUED:
        if in_state >= timedelta(seconds=config.dispatch_deadline_s):
            due.append(EnumLabJobDeadline.DISPATCH)
    elif row.state is _S.DISPATCHED:
        if in_state >= timedelta(seconds=config.claim_deadline_s):
            due.append(EnumLabJobDeadline.CLAIM)
    elif row.state is _S.STOPPING:
        if in_state >= timedelta(seconds=config.stop_deadline_s):
            due.append(EnumLabJobDeadline.STOP)
    elif (
        row.state is _S.ALERTING
        and row.alert_sent_at is not None
        and now - row.alert_sent_at >= timedelta(seconds=config.alert_deadline_s)
    ):
        due.append(EnumLabJobDeadline.ALERT)
    if (
        row.state in (_S.QUEUED, _S.RETRYING)
        and row.next_dispatch_at is not None
        and now >= row.next_dispatch_at
    ):
        due.append(EnumLabJobDeadline.BACKOFF)
    return tuple(due)


__all__: list[str] = ["elapsed_deadlines"]
