# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The convergence orchestrator node (OMN-24198).

On a runtime tick sends one convergence command per declared host as a lab work unit and ends with one
typed result per host; in observe mode the command only reads the clones.
"""
