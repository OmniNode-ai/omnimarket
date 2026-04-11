# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for the memory retrieval handler.

HandlerMemoryRetrieval.

.. versionadded:: 0.1.0
    Initial implementation for OMN-1387.
.. versionchanged:: 0.1.0
    OMN-4474: renamed use_mock_handlers -> use_stub_handlers; added
    qdrant_config field for production Qdrant handler configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model_handler_db_mock_config import ModelHandlerDbMockConfig
from .model_handler_graph_mock_config import ModelHandlerGraphMockConfig
from .model_handler_qdrant_mock_config import ModelHandlerQdrantMockConfig

if TYPE_CHECKING:
    from .model_handler_qdrant_config import ModelHandlerQdrantConfig

__all__ = [
    "ModelHandlerMemoryRetrievalConfig",
]


class ModelHandlerMemoryRetrievalConfig(BaseModel):
    """Configuration for the memory retrieval handler.

    Attributes:
        qdrant_config: Production Qdrant handler configuration. Required when
            use_stub_handlers is False.
        qdrant_mock_config: Configuration for the mock Qdrant handler (used
            when use_stub_handlers is True).
        db_config: Configuration for the Database handler.
        graph_config: Configuration for the Graph handler.
        use_stub_handlers: Whether to use stub (mock) handlers. When False,
            production handlers are used and qdrant_config must be provided.

    Raises:
        ValueError: If use_stub_handlers is False but qdrant_config is not provided.
    """

    model_config = ConfigDict(frozen=True)

    qdrant_config: ModelHandlerQdrantConfig | None = Field(
        default=None,
        description="Production Qdrant handler config — required when use_stub_handlers=False",
    )
    qdrant_mock_config: ModelHandlerQdrantMockConfig = Field(
        default_factory=ModelHandlerQdrantMockConfig,
        description="Mock Qdrant handler config — used when use_stub_handlers=True",
    )
    db_config: ModelHandlerDbMockConfig = Field(
        default_factory=ModelHandlerDbMockConfig,
        description="Configuration for Database full-text search handler",
    )
    graph_config: ModelHandlerGraphMockConfig = Field(
        default_factory=ModelHandlerGraphMockConfig,
        description="Configuration for Graph traversal handler",
    )
    use_stub_handlers: bool = Field(
        default=True,
        description="Use stub (mock) handlers (True) or production handlers (False)",
    )

    @model_validator(mode="after")
    def validate_qdrant_config_required_for_production(self) -> Self:  # stub-ok  # fmt: skip
        """Validate that qdrant_config is provided when use_stub_handlers is False.

        Returns:
            Self: The validated model instance.

        Raises:
            ValueError: If use_stub_handlers is False but qdrant_config is None.
        """
        if not self.use_stub_handlers and self.qdrant_config is None:
            raise ValueError("qdrant_config is required when use_stub_handlers=False")
        return self


# Resolve forward reference for ModelHandlerQdrantConfig at runtime.
# The TYPE_CHECKING import above satisfies static analysis; this import
# is required so Pydantic can resolve the annotation during model_rebuild().
from .model_handler_qdrant_config import ModelHandlerQdrantConfig  # noqa: E402

ModelHandlerMemoryRetrievalConfig.model_rebuild()
