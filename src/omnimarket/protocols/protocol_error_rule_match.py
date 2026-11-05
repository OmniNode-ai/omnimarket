# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dependency boundaries for replay and the exception emit verb."""

from typing import Protocol

from omnimarket.models.error_rule_match import (
    ModelErrorRuleMatchRequest,
    ModelErrorRuleMatchResult,
)
from omnimarket.models.exception_queue import (
    ModelExceptionEmitRequest,
    ModelExceptionEmitResult,
)


class ProtocolErrorRuleMatcher(Protocol):
    def handle(
        self, request: ModelErrorRuleMatchRequest
    ) -> ModelErrorRuleMatchResult: ...


class ProtocolErrorRuleExceptionEmitter(Protocol):
    def handle(
        self, request: ModelExceptionEmitRequest
    ) -> ModelExceptionEmitResult: ...
