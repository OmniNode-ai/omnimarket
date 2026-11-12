# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models of the rerun-request seam: the request any lane publishes and the typed answer (OMN-23830).

Shared so the ``onex scm rerun-request`` verb, the landing tick orchestrator and the rerun decision node
import one definition and a seam test fails when either side changes shape.
"""
