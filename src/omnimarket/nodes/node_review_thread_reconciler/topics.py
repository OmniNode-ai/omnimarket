# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_review_thread_reconciler.

Declared in contract.yaml event_bus section. Reference these constants in
handler code — never inline topic strings directly.

Related:
    - OMN-8493: Reconciler webhook — re-open threads resolved by non-bot actors
"""

from __future__ import annotations

TOPIC_CMD_RECONCILE = "onex.cmd.omnimarket.review-thread-reconcile.v1"
TOPIC_EVT_THREAD_REOPENED = "onex.evt.omnimarket.review-thread-reopened.v1"

__all__: list[str] = [
    "TOPIC_CMD_RECONCILE",
    "TOPIC_EVT_THREAD_REOPENED",
]
