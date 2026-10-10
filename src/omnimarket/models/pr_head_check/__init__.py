# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared input models of the head-check classifier (OMN-20866).

Promoted out of node_pr_lifecycle_triage_compute's private models package so
the PR landing orchestrator builds the classifier's facts without reaching into
that node (OMN-9263). The verdict side lives in ``omnimarket.events.pr_head_check``.
"""
