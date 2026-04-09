# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node role enumeration for composable pipeline decomposition."""

from __future__ import annotations

from enum import StrEnum


class EnumNodeRole(StrEnum):
    """Architectural role of a node within a domain pipeline.

    Used to declare the functional position of a node in a composable
    decomposition, enabling catalog generation, dependency analysis,
    and pipeline visualization.

    Values:
        INVENTORY: Discovers and lists existing resources (read-only scan).
        TRIAGE: Classifies or prioritizes items for downstream processing.
        FIX: Applies corrections or remediations to identified issues.
        PROBE: Performs health checks, assertions, or exploratory queries.
        REPORT: Aggregates findings and generates structured output.
        ORCHESTRATOR: Coordinates other nodes; emits intents, no result.
        REDUCER: Pure state transition; delta(state, event) -> (new_state, intents[]).
        EFFECT: Executes side effects (I/O, API calls, mutations).
        INTERNAL: Internal implementation detail; not part of public pipeline surface.
    """

    INVENTORY = "inventory"
    TRIAGE = "triage"
    FIX = "fix"
    PROBE = "probe"
    REPORT = "report"
    ORCHESTRATOR = "orchestrator"
    REDUCER = "reducer"
    EFFECT = "effect"
    INTERNAL = "internal"
