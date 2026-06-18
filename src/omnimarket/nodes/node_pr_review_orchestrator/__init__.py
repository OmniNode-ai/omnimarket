# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_review_orchestrator — canonical PR review orchestrator (OMN-13212 / B2).

Rebuilt from the deleted node_pr_review_bot ``workflow`` shell. Coordinates the
github-diff EFFECT, inference fan-out, finding aggregation, github-review EFFECT,
and judge verification over the bus; folds the FSM via the pure pr-review FSM
helpers; emits the ReviewVerdict completed event.
"""
