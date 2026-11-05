# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stop gate wire models. Validation belongs to the core boundary, not this hook."""

from __future__ import annotations

from dataclasses import dataclass

from ....models.model_operator_stop_gate import (
    ModelAnswerHandoffRequest as ModelAnswerHandoffRequest,
)
from ....models.model_operator_stop_gate import (
    ModelAnswerHandoffResult as ModelAnswerHandoffResult,
)
from ....models.model_operator_stop_gate import (
    ModelUnansweredGuardRequest as ModelUnansweredGuardRequest,
)
from ....models.model_operator_stop_gate import (
    ModelUnansweredGuardResult as ModelUnansweredGuardResult,
)


@dataclass(frozen=True, slots=True)
class ModelUnansweredGuardPolicy:
    max_blocks: int
    budget_ms: float
    counter_poll_seconds: float
    overlay_env: str
    arrival_overlay_env: str
    handoff_command_env: str
    handoff_timeout_seconds: float
    alarm_kind: str
    acknowledgement_kind: str
    source: str
    detector_contract: str
    detector_handler: str
    detector_input_model: str
    detector_command_topic: str
    detector_terminal_topic: str
    dependency_packages: tuple[str, ...]
    dependency_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelGuardSnapshot:
    unanswered: tuple[tuple[str, str | None], ...]
    answered_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelGuardRow:
    kind: str
    session_id: str
    message_ids: tuple[str, ...]
    owner: str = ""
    cause: str = ""
    words: str | None = None
    detail: str = ""
