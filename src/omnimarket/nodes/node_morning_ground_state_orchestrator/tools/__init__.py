# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic tools for the morning phase agents (OMN-17427).

RULING 2026-10-21T22:32:15Z: a morning phase agent gets no shell and no generic tool,
not even an allowlisted one. Each phase is given a fixed set of specific, typed
operations, each doing one thing, and every effect is done by the tool, never by the
agent. ``catalog`` holds the operations, ``phase_tools.json`` names the set each phase
gets, and ``server`` serves exactly one phase's set to one agent session.
"""
