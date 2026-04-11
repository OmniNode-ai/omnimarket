# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_pr_review_bot.

Declared in contract.yaml publish_topics. Reference these constants in
handler code — never inline topic strings directly.

Related:
    - OMN-7963: PR Review Bot epic
    - OMN-8497: Emergency bypass parser
"""

from __future__ import annotations

TOPIC_PHASE_TRANSITION = "onex.evt.omnimarket.pr-review-bot-phase-transition.v1"
TOPIC_THREAD_POSTED = "onex.evt.omnimarket.pr-review-bot-thread-posted.v1"
TOPIC_THREAD_VERIFIED = "onex.evt.omnimarket.pr-review-bot-thread-verified.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.pr-review-bot-completed.v1"
TOPIC_BYPASS_USED = "onex.evt.omnimarket.review-bot-bypass-used.v1"
TOPIC_BYPASS_ROLLED_BACK = "onex.evt.omnimarket.review-bot-bypass-rolled-back.v1"
