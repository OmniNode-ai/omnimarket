# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retired-model reference check effect node."""

from .handlers.handler_retired_model_reference_check import (
    HandlerRetiredModelReferenceCheck,
)


class NodeRetiredModelReferenceCheckEffect(HandlerRetiredModelReferenceCheck):
    """ONEX entrypoint for the repository reference gate."""


__all__ = ["HandlerRetiredModelReferenceCheck", "NodeRetiredModelReferenceCheckEffect"]
