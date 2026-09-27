# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumHeadCheckVerdict: the PR-level verdict over one head's check-runs.

The landing workflow's reducer reads exactly one of these per head while the
PR sits in CHECKS_PENDING and maps it to one intent:

- ``GREEN``: every required context's newest copy passed. Arm or enqueue.
- ``PENDING``: a required context has not finished. Read again later.
- ``CHANGE_CONTROL_OPEN``: only change-control contexts block, and the
  change-control companion has not merged. Re-running is useless until it
  does.
- ``CHANGE_CONTROL_STALE``: only change-control contexts are red, and the
  companion has merged. Re-run the named runs.
- ``TIMED_OUT``: a required context timed out with no product failure.
  Re-run the named runs.
- ``RUNNER_INFRA``: a required context failed on the runner or its
  environment, not on the product. Re-run the named runs.
- ``CANCELLED``: a required context was cancelled with no product failure.
  Re-run the named runs.
- ``STALE_CALLER_PIN``: a red whose re-run would replay the old
  reusable-workflow pin, because the failing workflow's caller file changed
  on the base since the head's merge base. The remedy is update-branch,
  never a re-run.
- ``BEHIND_REQUIRED``: the base requires an up-to-date branch and the head
  is behind it. The remedy is update-branch.
- ``PRODUCT_FAILED``: a real failure in the PR's own code. An agent fixes
  it; nothing is re-run.
"""

from __future__ import annotations

from enum import StrEnum


class EnumHeadCheckVerdict(StrEnum):
    """The one PR-level verdict for a head's check-runs."""

    GREEN = "green"
    PENDING = "pending"
    CHANGE_CONTROL_OPEN = "change_control_open"
    CHANGE_CONTROL_STALE = "change_control_stale"
    TIMED_OUT = "timed_out"
    RUNNER_INFRA = "runner_infra"
    CANCELLED = "cancelled"
    STALE_CALLER_PIN = "stale_caller_pin"
    BEHIND_REQUIRED = "behind_required"
    PRODUCT_FAILED = "product_failed"


# The verdicts whose remedy is a re-run of named runs. Every other verdict's
# remedy is not a re-run, so its verdict names no check to re-run.
HEAD_CHECK_RERUN_VERDICTS: frozenset[EnumHeadCheckVerdict] = frozenset(
    {
        EnumHeadCheckVerdict.CHANGE_CONTROL_STALE,
        EnumHeadCheckVerdict.TIMED_OUT,
        EnumHeadCheckVerdict.RUNNER_INFRA,
        EnumHeadCheckVerdict.CANCELLED,
    }
)


__all__: list[str] = ["HEAD_CHECK_RERUN_VERDICTS", "EnumHeadCheckVerdict"]
