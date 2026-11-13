# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The classes an integration pass sorts a merged change into, and the typed failures of the eval."""

from __future__ import annotations

from enum import StrEnum


class EnumIntegrationClass(StrEnum):
    """Where a merged change stands after the hourly integration pass read it."""

    SEEN_RUNNING = "seen_running"
    NOT_RUNNING = "not_running"
    BLOCKED = "blocked"
    DEFECT = "defect"
    RUN_NOT_SEEN = "run_not_seen"


class EnumClassifyOperation(StrEnum):
    BUILD_SET = "build_set"
    SCORE_ANSWERS = "score_answers"


class EnumClassifyEvalFailure(StrEnum):
    OVERLAY_UNAVAILABLE = "overlay_unavailable"
    NO_LEDGER_LINES = "no_ledger_lines"
    CLASS_SHORTFALL = "class_shortfall"
    BELOW_MINIMUM_TOTAL = "below_minimum_total"
    NO_TASKS = "no_tasks"
    DUPLICATE_TASK = "duplicate_task"
    NO_ANSWERS = "no_answers"
    UNKNOWN_TASK = "unknown_task"
    DUPLICATE_ANSWER = "duplicate_answer"
    MISSING_PAYLOAD = "missing_payload"
