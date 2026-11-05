# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic wire envelopes for the exception queue's shared stdlib dataclasses."""

from pydantic import ConfigDict, RootModel

from omnimarket.models.exception_queue import (
    ModelExceptionEmitRequest,
    ModelExceptionEmitResult,
)


class ModelExceptionEmitWire(RootModel[ModelExceptionEmitRequest]):
    """The bus validates a JSON document into the queue's existing request type."""

    model_config = ConfigDict(frozen=True)


class ModelExceptionEmitWireResult(RootModel[ModelExceptionEmitResult]):
    """Expose the existing result as a canonical typed dispatch output."""

    model_config = ConfigDict(frozen=True)
