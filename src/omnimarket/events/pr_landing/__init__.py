# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models for the PR landing workflow's state, observation and intent models shared by its orchestrator and reducer.

Promoted out of the owning node's private models package so a sibling node
imports them from here, never by reaching into that node (OMN-9263).
"""
