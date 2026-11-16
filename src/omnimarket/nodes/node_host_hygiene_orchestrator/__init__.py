# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host hygiene orchestrator node (OMN-23707, part A).

Dispatches cleanup units to hosts based on disk pressure readings, using
the lab-work unit transport or an in-memory sender.
"""
