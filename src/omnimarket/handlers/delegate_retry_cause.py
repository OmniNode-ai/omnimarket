# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a delegate turn is worth one in-process retry (OMN-24006).

A deployed-lane turn that ended without an answer is retried once in process when
its receipt names one of three causes. A turn the lane consumer never started
(terminal_class queued_not_started) is its own cause, so the code edit loop can
keep its later turns off the queue that held it.
"""

from __future__ import annotations

from collections.abc import Mapping

QUEUED_NOT_STARTED_CAUSE = "was queued and never started"


def delegate_retry_cause(receipt: Mapping[str, object], null_terminal: bool) -> str:
    """The retry cause a delegate receipt names, or an empty string when it names none."""
    if (
        receipt.get("status") == "timeout"
        or receipt.get("terminal_failure_cause") == "timeout"
    ):
        return "timed out"
    if receipt.get("terminal_class") == "queued_not_started":
        return QUEUED_NOT_STARTED_CAUSE
    if receipt.get("terminal_failure_cause") == "no_terminal" or null_terminal:
        return "had no terminal"
    return ""
