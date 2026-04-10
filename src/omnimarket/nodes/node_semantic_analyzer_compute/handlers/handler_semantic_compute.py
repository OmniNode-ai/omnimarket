# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Semantic compute handler for semantic analysis operations.

embedding generation, and entity extraction. It depends on provider
protocols for I/O abstraction, keeping the handler testable and the
architecture clean.

Key Design Principles:
    - **Container-driven**: Handler receives ModelONEXContainer and resolves dependencies
    - **Pure compute**: Handler contains orchestration and transformation logic
    - **Protocol dependencies**: I/O is abstracted via ProtocolEmbeddingProvider
      and ProtocolLLMProvider
    - **Policy-driven**: SemanticComputePolicy makes runtime decisions
    - **Deterministic by default**: Reproducible results for testing

Operations:
    - **analyze**: Full semantic analysis (embedding + entities + topics)
    - **embed**: Generate embedding vector for content
    - **extract_entities**: Extract named entities from content

Example::

    from omnimarket.nodes.node_semantic_analyzer_compute.handlers import (
        HandlerSemanticCompute,
        ModelHandlerSemanticComputeConfig,
    )
    from omnibase_core.container import ModelONEXContainer

    # Container-driven pattern (recommended)
    container = ModelONEXContainer()
    container.register_singleton(ProtocolEmbeddingProvider, my_embedding_provider_factory)

    handler = HandlerSemanticCompute(container=container)
    await handler.initialize()

    # Or with explicit providers passed to initialize
    handler = HandlerSemanticCompute(container=container)
    await handler.initialize(
        config=ModelHandlerSemanticComputeConfig(),
        embedding_provider=my_embedding_provider,
        llm_provider=my_llm_provider,  # optional
    )

    # Generate embedding
    embedding = await handler.embed("Hello, world!")

    # Extract entities
    entities = await handler.extract_entities("John works at Google in NYC.")

    # Full analysis
    result = await handler.analyze("Complex content to analyze...")

    # Cleanup
    await handler.shutdown()

.. versionadded:: 0.1.0
    Initial implementation for OMN-1390.

.. versionchanged:: 0.2.0
    Refactored to container-driven pattern for OMN-1577.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID, uuid4

from cachetools import LRUCache
from omnimemory.enums import EnumEntityExtractionMode, EnumSemanticEntityType
from omnimemory.models.config import (
    ModelHandlerSemanticComputeConfig,
    ModelSemanticComputePolicyConfig,
)
from omnimemory.models.intelligence import (
    ModelSemanticAnalysisResult,
    ModelSemanticEntity,
    ModelSemanticEntityList,
)
from omnimemory.utils.handler_constants import (
    COMPLEXITY_SENTENCE_LEN_MIN,
    COMPLEXITY_SENTENCE_LEN_RANGE,
    COMPLEXITY_WORD_LEN_MIN,
    COMPLEXITY_WORD_LEN_RANGE,
    KEY_CONCEPT_CONFIDENCE_THRESHOLD,
    SENTENCE_STARTING_STOPWORDS,
    TOPIC_EXTRACTION_STOPWORDS,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer
    from omnimemory.protocols import ProtocolEmbeddingProvider, ProtocolLLMProvider

logger = logging.getLogger(__name__)

__all__ = [
    "HandlerSemanticCompute",
    "HandlerSemanticComputePolicy",
]

# TypeVar for generic retry helper
_T = TypeVar("_T")

# Health check timeout - shorter than operation timeout since health checks should be quick
_HEALTH_CHECK_TIMEOUT_SECONDS: float = 5.0

# Minimum cache size for LRUCache (cachetools requires maxsize > 0)
_MIN_CACHE_SIZE: int = 1


# =============================================================================
# Handler Metadata Models (handler-specific, not ONEX domain models)
# =============================================================================


class ModelSemanticComputeCapabilities(  # omnimemory-model-exempt: handler metadata
    BaseModel
):
    """Capabilities of the semantic compute handler.

    Attributes:
        embedding_generation: Whether embedding generation is supported.
        entity_extraction_heuristic: Whether heuristic entity extraction is supported.
        entity_extraction_llm: Whether LLM-based entity extraction is available.
        full_semantic_analysis: Whether full semantic analysis is supported.
        caching: Whether result caching is enabled.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    embedding_generation: bool = Field(
        ...,
        description="Whether embedding generation is supported",
    )
    entity_extraction_heuristic: bool = Field(
        ...,
        description="Whether heuristic entity extraction is supported",
    )
    entity_extraction_llm: bool = Field(
        ...,
        description="Whether LLM-based entity extraction is available",
    )
    full_semantic_analysis: bool = Field(
        ...,
        description="Whether full semantic analysis is supported",
    )
    caching: bool = Field(
        ...,
        description="Whether result caching is enabled",
    )


class ModelSemanticComputeConfigInfo(  # omnimemory-model-exempt: handler metadata
    BaseModel
):
    """Configuration info for the semantic compute handler metadata.

    Attributes:
        max_cache_size: Maximum number of cached items.
        entity_extraction_mode: Mode for entity extraction.
        is_deterministic: Whether handler operates in deterministic mode.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    max_cache_size: int = Field(
        ...,
        ge=0,
        description="Maximum number of cached items",
    )
    entity_extraction_mode: str = Field(
        ...,
        description="Mode for entity extraction (deterministic or best_effort)",
    )
    is_deterministic: bool = Field(
        ...,
        description="Whether handler operates in deterministic mode",
    )


class ModelSemanticComputeMetadata(  # omnimemory-model-exempt: handler metadata
    BaseModel
):
    """Metadata describing semantic compute handler capabilities and configuration.

    Returned by describe() method to provide introspection information
    about the handler's capabilities, operations, and current configuration.

    Attributes:
        name: Handler name identifier.
        version: Handler version string.
        initialized: Whether the handler has been initialized.
        operations: List of supported operations.
        capabilities: Handler capability flags.
        config: Current configuration information.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(
        ...,
        description="Handler name identifier",
    )
    version: str = Field(
        ...,
        description="Handler version string",
    )
    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )
    operations: list[str] = Field(
        ...,
        description="List of supported operations",
    )
    capabilities: ModelSemanticComputeCapabilities = Field(
        ...,
        description="Handler capability flags",
    )
    config: ModelSemanticComputeConfigInfo = Field(
        ...,
        description="Current configuration information",
    )


class ModelSemanticComputeHealth(  # omnimemory-model-exempt: handler health
    BaseModel
):
    """Health status for the Semantic Compute Handler.

    Returned by health_check() to provide detailed health information
    about the handler and its dependencies.

    Attributes:
        initialized: Whether the handler has been initialized.
        handler_name: Name identifier for this handler instance.
        handler_version: Semantic version of the handler.
        embedding_provider_healthy: Embedding provider health status.
        embedding_provider_name: Name of the configured embedding provider.
        embedding_provider_error: Error message if embedding provider is unhealthy.
        llm_provider_healthy: LLM provider health status.
        llm_provider_name: Name of the configured LLM provider.
        llm_provider_error: Error message if LLM provider is unhealthy.
        llm_provider_configured: Whether an LLM provider is configured.
        cache_size: Current number of cached embeddings.
        cache_max_size: Maximum cache capacity.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )
    handler_name: str | None = Field(
        default=None,
        description="Name identifier for this handler instance",
    )
    handler_version: str | None = Field(
        default=None,
        description="Semantic version of the handler",
    )
    embedding_provider_healthy: bool | None = Field(
        default=None,
        description="Embedding provider health status",
    )
    embedding_provider_name: str | None = Field(
        default=None,
        description="Name of the configured embedding provider",
    )
    embedding_provider_error: str | None = Field(
        default=None,
        description="Error message if embedding provider is unhealthy",
    )
    llm_provider_healthy: bool | None = Field(
        default=None,
        description="LLM provider health status",
    )
    llm_provider_name: str | None = Field(
        default=None,
        description="Name of the configured LLM provider",
    )
    llm_provider_error: str | None = Field(
        default=None,
        description="Error message if LLM provider is unhealthy",
    )
    llm_provider_configured: bool | None = Field(
        default=None,
        description="Whether an LLM provider is configured",
    )
    cache_size: int | None = Field(
        default=None,
        ge=0,
        description="Current number of cached embeddings",
    )
    cache_max_size: int | None = Field(
        default=None,
        ge=0,
        description="Maximum cache capacity",
    )


# =============================================================================
# Policy Class
# =============================================================================


class HandlerSemanticComputePolicy:
    """Policy class for semantic compute handler decisions.

    This class encapsulates decision logic based on the policy configuration.
    It determines retry behavior, fallback chains, caching strategies, and
    model selection.

    The policy is separate from configuration because:
    - Config is serializable data (knobs, thresholds)
    - Policy is executable logic (decisions, fallback chains)

    Example::

        policy = HandlerSemanticComputePolicy(config.policy_config)

        if policy.should_cache_embedding("hello world"):
            cache[key] = embedding

        if policy.should_retry(attempt=2, error=TimeoutError()):
            # retry the operation
    """

    def __init__(self, config: ModelSemanticComputePolicyConfig) -> None:
        """Initialize the policy with configuration.

        Args:
            config: The policy configuration containing thresholds and settings.
        """
        self._config = config

    @property
    def config(self) -> ModelSemanticComputePolicyConfig:
        """Get the policy configuration."""
        return self._config

    def should_cache_embedding(self, content: str) -> bool:
        """Determine if an embedding should be cached.

        Args:
            content: The content that was embedded.

        Returns:
            True if the embedding should be cached.
        """
        if not self._config.cache_embeddings:
            return False
        # Don't cache very short or very long content
        content_len = len(content)
        return 10 <= content_len <= self._config.max_content_length

    def should_retry(self, attempt: int, error: Exception) -> bool:
        """Determine if an operation should be retried.

        Args:
            attempt: Current attempt number (1-indexed).
            error: The error that occurred.

        Returns:
            True if the operation should be retried.
        """
        if attempt > self._config.max_retries:
            return False

        # Retry on transient errors
        error_name = type(error).__name__.lower()
        transient_indicators = ["timeout", "connection", "temporary", "retry"]
        return any(indicator in error_name for indicator in transient_indicators)

    def get_retry_delay_ms(self, attempt: int) -> int:
        """Calculate the retry delay for an attempt.

        Uses exponential backoff with jitter. The jitter behavior depends on
        the deterministic mode setting:

        - **Deterministic mode** (``is_deterministic=True``): Uses a seeded
          random number generator based on ``llm_seed`` and the attempt number.
          This ensures the same delay is returned for the same attempt across
          calls, making tests reproducible while still providing variation
          between different retry attempts.

        - **Non-deterministic mode** (``is_deterministic=False``): Uses the
          global random number generator for true randomness in jitter. This
          is preferred in production to prevent thundering herd issues when
          multiple clients retry simultaneously.

        Args:
            attempt: Current attempt number (1-indexed).

        Returns:
            Delay in milliseconds before the next retry.
        """
        base = self._config.retry_base_delay_ms
        max_delay = self._config.retry_max_delay_ms

        # Exponential backoff: base * 2^(attempt-1)
        delay = min(base * (2 ** (attempt - 1)), max_delay)

        # Apply jitter (full jitter pattern: multiply by random factor 0.5-1.0)
        # This helps prevent thundering herd when multiple clients retry simultaneously
        if self._config.is_deterministic:
            # In deterministic mode, use a seeded RNG for reproducible jitter.
            # Seed combines llm_seed (or 0 if None) with attempt number to ensure
            # different but reproducible jitter values for each retry attempt.
            seed_value = (self._config.effective_llm_seed or 0) + attempt
            seeded_rng = random.Random(seed_value)
            jitter_factor = seeded_rng.uniform(0.5, 1.0)
        else:
            # In non-deterministic mode, use global RNG for true randomness
            jitter_factor = random.uniform(0.5, 1.0)

        return int(delay * jitter_factor)

    def get_effective_llm_params(self) -> dict[str, float | int | None]:
        """Get effective LLM parameters based on extraction mode.

        Returns:
            Dictionary with temperature, seed, and max_tokens.
        """
        return {
            "temperature": self._config.effective_llm_temperature,
            "seed": self._config.effective_llm_seed,
            "max_tokens": self._config.max_tokens_per_request,
        }

    def filter_entities_by_confidence(
        self, entities: list[ModelSemanticEntity]
    ) -> list[ModelSemanticEntity]:
        """Filter entities by confidence threshold.

        Args:
            entities: List of extracted entities.

        Returns:
            Entities meeting the confidence threshold.
        """
        threshold = self._config.entity_confidence_threshold
        return [e for e in entities if e.confidence >= threshold]

    def should_use_llm_for_entities(self) -> bool:
        """Determine if LLM should be used for entity extraction.

        Returns:
            True if LLM-based extraction is preferred.
        """
        # For deterministic mode, prefer heuristic extraction
        # For best_effort mode, use LLM if available
        return (
            self._config.entity_extraction_mode == EnumEntityExtractionMode.BEST_EFFORT
        )


# =============================================================================
# Handler Implementation
# =============================================================================


class HandlerSemanticCompute:
    """Pure compute handler for semantic analysis operations.

    - Embedding generation via ProtocolEmbeddingProvider
    - Entity extraction (heuristic or LLM-backed)
    - Full semantic analysis combining embeddings, entities, and topics

    The handler is "pure compute" in the ONEX sense: it orchestrates
    transformations and delegates I/O to injected provider protocols.

    Following the container-driven pattern (OMN-1577):
    - Constructor takes only ModelONEXContainer
    - Providers are resolved from container or passed to initialize()
    - Lifecycle methods: initialize(), health_check(), describe(), shutdown()

    Attributes:
        container: The ONEX container for dependency injection.
        config: Handler configuration (after initialization).
        policy: Policy for runtime decisions (after initialization).

    Example::

        # Container-driven pattern
        container = ModelONEXContainer()
        handler = HandlerSemanticCompute(container=container)
        await handler.initialize(embedding_provider=my_provider)

        # Generate embedding
        embedding = await handler.embed("Hello world")

        # Full analysis
        result = await handler.analyze("Analyze this text for insights.")

        # Check health
        health = await handler.health_check()

        # Cleanup
        await handler.shutdown()
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        """Initialize the semantic compute handler with container.

        The handler is not ready for use until initialize() is called.
        This follows the container-driven pattern where:
        - Constructor only stores the container reference
        - initialize() resolves dependencies and sets up state

        Args:
            container: ONEX container for dependency injection.
        """
        self._container = container
        self._config: ModelHandlerSemanticComputeConfig | None = None
        self._embedding_provider: ProtocolEmbeddingProvider | None = None
        self._llm_provider: ProtocolLLMProvider | None = None
        self._policy: HandlerSemanticComputePolicy | None = None
        self._embedding_cache: LRUCache[str, list[float]] | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(
        self,
        config: ModelHandlerSemanticComputeConfig | None = None,
        embedding_provider: ProtocolEmbeddingProvider | None = None,
        llm_provider: ProtocolLLMProvider | None = None,
    ) -> None:
        """Initialize the handler by resolving dependencies.

        Dependencies can be:
        1. Passed explicitly to this method (highest priority)
        2. Resolved from the container if registered
        3. Default config is created if not provided

        Args:
            config: Optional handler configuration. Defaults to
                ModelHandlerSemanticComputeConfig() if not provided.
            embedding_provider: Optional embedding provider. If not provided,
                attempts to resolve ProtocolEmbeddingProvider from container.
            llm_provider: Optional LLM provider. If not provided,
                attempts to resolve ProtocolLLMProvider from container.

        Raises:
            RuntimeError: If embedding_provider is not provided and not
                registered in container (embedding provider is required).
        """
        # Fast path: already initialized (avoid lock acquisition)
        if self._initialized:
            return

        async with self._init_lock:
            # Double-check after acquiring lock
            if self._initialized:
                return

            # Import here to avoid circular imports at module level
            from omnimemory.protocols import (
                ProtocolEmbeddingProvider,
                ProtocolLLMProvider,
            )

            # Resolve config
            self._config = config or ModelHandlerSemanticComputeConfig()

            # Resolve embedding provider (required)
            if embedding_provider is not None:
                self._embedding_provider = embedding_provider
            else:
                resolved = self._container.get_service_optional(
                    ProtocolEmbeddingProvider  # type: ignore[type-abstract]
                )
                if resolved is not None:
                    self._embedding_provider = resolved
                else:
                    raise RuntimeError(
                        "HandlerSemanticCompute requires an embedding provider. "
                        "Either pass embedding_provider to initialize() or register "
                        "ProtocolEmbeddingProvider in the container."
                    )

            # Resolve LLM provider (optional)
            if llm_provider is not None:
                self._llm_provider = llm_provider
            else:
                self._llm_provider = self._container.get_service_optional(
                    ProtocolLLMProvider  # type: ignore[type-abstract]
                )

            # Set up policy and cache
            self._policy = HandlerSemanticComputePolicy(self._config.policy_config)

            # Guard against max_cache_size=0: cachetools LRUCache requires maxsize > 0
            # If caching is disabled (max_cache_size=0), use minimum size but caching
            # will be bypassed by enable_caching=False or the cache will just evict quickly
            effective_cache_size = max(self._config.max_cache_size, _MIN_CACHE_SIZE)
            self._embedding_cache = LRUCache(maxsize=effective_cache_size)

            self._initialized = True
            logger.debug(
                "HandlerSemanticCompute initialized: embedding_provider=%s, llm_provider=%s",
                self._embedding_provider.provider_name
                if self._embedding_provider
                else None,
                self._llm_provider.provider_name if self._llm_provider else None,
            )

    async def health_check(self) -> ModelSemanticComputeHealth:
        """Check handler health and return status.

        Returns:
            ModelSemanticComputeHealth with detailed health information including:
            - initialized: Whether handler is initialized
            - embedding_provider_healthy: Embedding provider health (if initialized)
            - llm_provider_healthy: LLM provider health (if configured)
            - cache_size: Current cache size
            - cache_max_size: Maximum cache size

        Example::

            health = await handler.health_check()
            if health.initialized and health.embedding_provider_healthy:
                print("Handler is ready")
        """
        # Build health status with typed model
        embedding_provider_healthy: bool | None = None
        embedding_provider_name: str | None = None
        embedding_provider_error: str | None = None
        llm_provider_healthy: bool | None = None
        llm_provider_name: str | None = None
        llm_provider_error: str | None = None
        llm_provider_configured: bool = self._llm_provider is not None
        cache_size: int | None = None
        cache_max_size: int | None = None

        if self._initialized and self._embedding_provider is not None:
            try:
                # Use explicit timeout to prevent health check from hanging
                embedding_provider_healthy = await asyncio.wait_for(
                    self._embedding_provider.health_check(),
                    timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
                )
                embedding_provider_name = self._embedding_provider.provider_name
            except TimeoutError:
                embedding_provider_healthy = False
                embedding_provider_error = (
                    f"Health check timed out after {_HEALTH_CHECK_TIMEOUT_SECONDS}s"
                )
            except Exception as e:
                embedding_provider_healthy = False
                embedding_provider_error = str(e)

        if self._initialized and self._llm_provider is not None:
            try:
                # Use explicit timeout to prevent health check from hanging
                llm_provider_healthy = await asyncio.wait_for(
                    self._llm_provider.health_check(),
                    timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
                )
                llm_provider_name = self._llm_provider.provider_name
            except TimeoutError:
                llm_provider_healthy = False
                llm_provider_error = (
                    f"Health check timed out after {_HEALTH_CHECK_TIMEOUT_SECONDS}s"
                )
            except Exception as e:
                llm_provider_healthy = False
                llm_provider_error = str(e)

        if self._embedding_cache is not None:
            cache_size = len(self._embedding_cache)
            cache_max_size = int(self._embedding_cache.maxsize)

        return ModelSemanticComputeHealth(
            initialized=self._initialized,
            handler_name=self._config.handler_name if self._config else None,
            handler_version=str(self._config.handler_version) if self._config else None,
            embedding_provider_healthy=embedding_provider_healthy,
            embedding_provider_name=embedding_provider_name,
            embedding_provider_error=embedding_provider_error,
            llm_provider_healthy=llm_provider_healthy,
            llm_provider_name=llm_provider_name,
            llm_provider_error=llm_provider_error,
            llm_provider_configured=llm_provider_configured,
            cache_size=cache_size,
            cache_max_size=cache_max_size,
        )

    async def describe(self) -> ModelSemanticComputeMetadata:
        """Return handler metadata and capabilities.

        Returns:
            ModelSemanticComputeMetadata with handler information including
            name, version, operations, capabilities, and configuration.

        Example::

            metadata = await handler.describe()
            print(f"Handler: {metadata.name} v{metadata.version}")
        """
        return ModelSemanticComputeMetadata(
            name=self._config.handler_name if self._config else "semantic-compute",
            version=str(self._config.handler_version) if self._config else "1.0.0",
            initialized=self._initialized,
            operations=["embed", "extract_entities", "analyze"],
            capabilities=ModelSemanticComputeCapabilities(
                embedding_generation=True,
                entity_extraction_heuristic=True,
                entity_extraction_llm=self._llm_provider is not None,
                full_semantic_analysis=True,
                caching=self._config.enable_caching if self._config else True,
            ),
            config=ModelSemanticComputeConfigInfo(
                max_cache_size=self._config.max_cache_size if self._config else 1000,
                entity_extraction_mode=(
                    self._config.policy_config.entity_extraction_mode.value
                    if self._config
                    else "deterministic"
                ),
                is_deterministic=(
                    self._config.policy_config.is_deterministic
                    if self._config
                    else True
                ),
            ),
        )

    async def shutdown(self) -> None:
        """Clean up handler resources.

        Clears the embedding cache and resets state.
        After shutdown, initialize() must be called again to use the handler.

        Example::

            await handler.shutdown()
            # Handler is no longer usable until initialize() is called
        """
        if self._embedding_cache is not None:
            self._embedding_cache.clear()
        self._initialized = False
        logger.debug("HandlerSemanticCompute shutdown complete")

    def _ensure_initialized(self) -> None:
        """Ensure the handler is initialized before operations.

        Raises:
            RuntimeError: If handler is not initialized.
        """
        if not self._initialized:
            raise RuntimeError(
                "HandlerSemanticCompute is not initialized. "
                "Call initialize() before using the handler."
            )

    @property
    def container(self) -> ModelONEXContainer:
        """Get the ONEX container."""
        return self._container

    @property
    def config(self) -> ModelHandlerSemanticComputeConfig:
        """Get the handler configuration.

        Raises:
            RuntimeError: If handler is not initialized.
        """
        self._ensure_initialized()
        assert self._config is not None  # For type checker
        return self._config

    @property
    def policy(self) -> HandlerSemanticComputePolicy:
        """Get the policy instance.

        Raises:
            RuntimeError: If handler is not initialized.
        """
        self._ensure_initialized()
        assert self._policy is not None  # For type checker
        return self._policy

    @property
    def embedding_provider(self) -> ProtocolEmbeddingProvider:
        """Get the embedding provider.

        Raises:
            RuntimeError: If handler is not initialized.
        """
        self._ensure_initialized()
        assert self._embedding_provider is not None  # For type checker
        return self._embedding_provider

    @property
    def llm_provider(self) -> ProtocolLLMProvider | None:
        """Get the LLM provider (may be None).

        Raises:
            RuntimeError: If handler is not initialized.
        """
        self._ensure_initialized()
        return self._llm_provider

    @property
    def is_initialized(self) -> bool:
        """Check if handler is initialized."""
        return self._initialized

    # =========================================================================
    # Retry Logic
    # =========================================================================

    async def _execute_with_retry(
        self,
        operation: Callable[[], Awaitable[_T]],
        operation_name: str,
    ) -> _T:
        """Execute an operation with retry logic based on policy.

        Uses the policy's should_retry() and get_retry_delay_ms() methods
        to determine retry behavior. Retries are only attempted for transient
        errors (timeout, connection issues).

        Args:
            operation: Async callable to execute (no arguments).
            operation_name: Name for logging purposes.

        Returns:
            The operation result.

        Raises:
            The last exception if all retries are exhausted, or immediately
            for non-transient errors.

        Note:
            Assumes handler is initialized (caller should check).
        """
        assert self._config is not None  # For type checker
        assert self._policy is not None  # For type checker

        last_error: Exception | None = None
        max_attempts = self._config.policy_config.max_retries + 1  # initial + retries

        for attempt in range(1, max_attempts + 1):
            try:
                return await operation()
            except Exception as e:
                last_error = e

                # Check if we should retry
                if not self._policy.should_retry(attempt, e):
                    logger.debug(
                        "Not retrying %s after attempt %d: %s (non-transient error)",
                        operation_name,
                        attempt,
                        type(e).__name__,
                    )
                    raise

                # Check if we have more attempts left
                if attempt >= max_attempts:
                    logger.warning(
                        "All %d retry attempts exhausted for %s: %s",
                        max_attempts,
                        operation_name,
                        str(e),
                    )
                    raise

                delay_ms = self._policy.get_retry_delay_ms(attempt)
                logger.warning(
                    "Retry %d/%d for %s after %dms: %s",
                    attempt,
                    self._config.policy_config.max_retries,
                    operation_name,
                    delay_ms,
                    str(e),
                )
                await asyncio.sleep(delay_ms / 1000.0)

        # Should not reach here, but satisfy type checker
        if last_error:
            raise last_error
        raise RuntimeError(f"Unexpected state in retry logic for {operation_name}")

    # =========================================================================
    # Core Operations
    # =========================================================================

    async def embed(
        self,
        content: str,
        *,
        model: str | None = None,
        correlation_id: UUID | None = None,
    ) -> list[float]:
        """Generate embedding vector for content.

        Args:
            content: The text content to embed.
            model: Optional model override.
            correlation_id: Optional correlation ID for tracing.

        Returns:
            Embedding vector as list of floats.

        Raises:
            ValueError: If content is empty or too long.
            RuntimeError: If handler is not initialized.
            EmbeddingProviderError: If embedding generation fails.
        """
        self._ensure_initialized()
        assert self._config is not None  # For type checker
        assert self._embedding_provider is not None  # For type checker
        assert self._policy is not None  # For type checker
        assert self._embedding_cache is not None  # For type checker

        # Validate content
        if not content or not content.strip():
            raise ValueError("Content cannot be empty")

        content_len = len(content)
        max_len = self._config.policy_config.max_content_length
        if content_len > max_len:
            raise ValueError(
                f"Content length ({content_len}) exceeds maximum ({max_len})"
            )

        # Check cache
        cache_key = self._compute_cache_key(content, model)
        if self._config.enable_caching and cache_key in self._embedding_cache:
            return list(self._embedding_cache[cache_key])

        # Generate embedding via provider with retry logic for transient failures
        timeout = self._config.policy_config.timeout_seconds
        effective_model = model or self._config.policy_config.default_embedding_model

        async def _do_embed() -> list[float]:
            async with asyncio.timeout(timeout):
                assert (
                    self._embedding_provider is not None
                )  # For type checker in closure
                return await self._embedding_provider.generate_embedding(
                    text=content,
                    model=effective_model,
                    correlation_id=correlation_id,
                    timeout_seconds=timeout,
                )

        embedding = await self._execute_with_retry(_do_embed, "embed")

        # Cache if appropriate (LRUCache handles eviction automatically)
        if self._config.enable_caching and self._policy.should_cache_embedding(content):
            self._embedding_cache[cache_key] = embedding

        return embedding

    async def extract_entities(
        self,
        content: str,
        *,
        correlation_id: UUID | None = None,
    ) -> ModelSemanticEntityList:
        """Extract named entities from content.

        The extraction strategy is determined by ``entity_extraction_mode`` in
        the policy configuration:

        **DETERMINISTIC mode** (default):
            Uses ``_extract_entities_heuristic()`` for simple, reproducible
            extraction. Best for testing and when external dependencies are
            not desired.

            Strengths:
                - No external API calls (fast, offline-capable)
                - Deterministic results (same input = same output)
                - No LLM provider required

            Limitations:
                - Only detects single capitalized words
                - Cannot extract multi-word entities ("New York" split)
                - Cannot extract dates, times, or numbers
                - Acronyms classified as MISC (not semantic type)
                - No context awareness

        **BEST_EFFORT mode**:
            Uses LLM-backed extraction via ``_extract_entities_llm()`` for
            higher accuracy. Requires an LLM provider to be configured.

            Strengths:
                - Multi-word entity detection ("New York City" as one entity)
                - Date, time, and numeric extraction
                - Context-aware classification ("Apple" as ORG vs fruit)
                - Better acronym handling ("NYC" as LOCATION)

            Limitations:
                - Requires LLM provider (external dependency)
                - Non-deterministic (results may vary)
                - Higher latency (API calls)
                - Potential cost implications

        Example::

            # DETERMINISTIC mode (default) - fast, limited accuracy
            container = ModelONEXContainer()
            handler = HandlerSemanticCompute(container=container)
            await handler.initialize(
                config=ModelHandlerSemanticComputeConfig(
                    policy_config=ModelSemanticComputePolicyConfig(
                        entity_extraction_mode=EnumEntityExtractionMode.DETERMINISTIC,
                    )
                ),
                embedding_provider=provider,
            )
            # "NYC" -> MISC, "New York City" -> "New", "York", "City" separately

            # BEST_EFFORT mode - higher accuracy, requires LLM
            container = ModelONEXContainer()
            handler = HandlerSemanticCompute(container=container)
            await handler.initialize(
                config=ModelHandlerSemanticComputeConfig(
                    policy_config=ModelSemanticComputePolicyConfig(
                        entity_extraction_mode=EnumEntityExtractionMode.BEST_EFFORT,
                    )
                ),
                embedding_provider=provider,
                llm_provider=llm_provider,  # Required for BEST_EFFORT
            )
            # "NYC" -> LOCATION, "New York City" -> single LOCATION entity

        Args:
            content: The text content to analyze.
            correlation_id: Optional correlation ID for tracing.

        Returns:
            ModelSemanticEntityList with extracted entities, including metadata
            about the extraction method used (``extraction_model`` field).

        Raises:
            ValueError: If content is empty.
            RuntimeError: If handler is not initialized, or if
                ``entity_extraction_mode=BEST_EFFORT`` but no LLM provider is configured.
        """
        self._ensure_initialized()
        assert self._config is not None  # For type checker
        assert self._policy is not None  # For type checker

        if not content or not content.strip():
            raise ValueError("Content cannot be empty")

        # Determine extraction strategy
        policy_wants_llm = self._policy.should_use_llm_for_entities()

        if policy_wants_llm:
            # Fail fast if LLM is required but not configured
            if not self._llm_provider:
                raise RuntimeError(
                    "LLM provider not configured but LLM entity extraction requested "
                    "(entity_extraction_mode=BEST_EFFORT requires an LLM provider)"
                )
            entities = await self._extract_entities_llm(content, correlation_id)
        else:
            logger.debug(
                "Using heuristic entity extraction (deterministic mode). "
                "Note: Limited to single capitalized words. For multi-word entities, "
                "dates, and context-aware classification, use entity_extraction_mode=BEST_EFFORT."
            )
            start_time = time.perf_counter()
            entities = self._extract_entities_heuristic(content)
            elapsed = time.perf_counter() - start_time

            # Warn if heuristic took unexpectedly long (shouldn't happen)
            if elapsed > self._config.policy_config.timeout_seconds:
                logger.warning(
                    "Heuristic entity extraction took %.2fs, exceeding timeout of %.2fs",
                    elapsed,
                    self._config.policy_config.timeout_seconds,
                )

        # Filter by confidence
        filtered_entities = self._policy.filter_entities_by_confidence(entities)

        # Limit number of entities
        max_entities = self._config.policy_config.max_entities_per_request
        if len(filtered_entities) > max_entities:
            filtered_entities = filtered_entities[:max_entities]

        return ModelSemanticEntityList(
            entities=filtered_entities,
            source_text_length=len(content),
            extraction_model="llm" if policy_wants_llm else "heuristic",
            is_deterministic=self._config.policy_config.is_deterministic,
        )

    async def analyze(
        self,
        content: str,
        *,
        analysis_type: str = "full",
        correlation_id: UUID | None = None,
    ) -> ModelSemanticAnalysisResult:
        """Perform full semantic analysis on content.

        Combines embedding generation, entity extraction, and topic analysis
        into a comprehensive semantic analysis result.

        Args:
            content: The text content to analyze.
            analysis_type: Type of analysis ("full", "embedding_only", "entities_only").
            correlation_id: Optional correlation ID for tracing.

        Returns:
            ModelSemanticAnalysisResult with analysis data.

        Raises:
            ValueError: If content is empty or analysis_type is invalid.

        Note:
            **Topic Extraction Limitations**:

            The ``topics`` field uses ``_extract_topics_heuristic()``, a simple
            word-frequency approach that does NOT perform semantic topic modeling.

            How it works:
                - Converts text to lowercase and splits into words
                - Filters common stopwords (the, is, are, etc.) and short words (<4 chars)
                - Counts frequency of remaining words
                - Returns the top 5 most frequent words as "topics"

            Limitations:
                - **No semantic understanding**: Topics are just frequent words, not
                  conceptual themes. "Python" appearing 5 times becomes a topic regardless
                  of context (programming language vs snake).
                - **No multi-word topics**: Cannot extract phrases like "machine learning"
                  or "climate change" as single topics.
                - **No topic modeling algorithms**: Does NOT use LDA (Latent Dirichlet
                  Allocation), NMF, or embeddings-based clustering.
                - **No document-level coherence**: Topics are based solely on word
                  frequency, not semantic relationships between concepts.
                - **Stopword-dependent quality**: Topic quality depends heavily on the
                  hardcoded stopword list; domain-specific common words may pollute results.

            Use Cases:
                - Quick keyword extraction for indexing
                - Testing and development (deterministic, reproducible)
                - Lightweight analysis where precision is not critical

            For sophisticated topic analysis (semantic topic modeling, multi-word
            topic extraction, or hierarchical topic structures), consider integrating
            external NLP services or dedicated topic modeling libraries (gensim, BERTopic).

            **Entity Extraction**: See :meth:`extract_entities` for detailed documentation
            on entity extraction modes, capabilities, and limitations.

        Raises:
            ValueError: If content is empty or analysis_type is invalid.
            RuntimeError: If handler is not initialized.
        """
        self._ensure_initialized()
        assert self._config is not None  # For type checker
        assert self._embedding_provider is not None  # For type checker

        if not content or not content.strip():
            raise ValueError("Content cannot be empty")

        valid_types = {"full", "embedding_only", "entities_only"}
        if analysis_type not in valid_types:
            raise ValueError(
                f"Invalid analysis_type '{analysis_type}'. Must be one of: {valid_types}"
            )

        correlation_id = correlation_id or uuid4()
        start_time = time.perf_counter()

        # Initialize result components
        embedding: list[float] = []
        entities: list[str] = []
        entity_list: ModelSemanticEntityList | None = None
        topics: list[str] = []
        key_concepts: list[str] = []

        # Generate embedding
        if analysis_type in {"full", "embedding_only"}:
            embedding = await self.embed(content, correlation_id=correlation_id)

        # Extract entities - store full list for the result
        if analysis_type in {"full", "entities_only"}:
            entity_list = await self.extract_entities(
                content, correlation_id=correlation_id
            )
            entities = [e.text for e in entity_list.entities]

            # Extract key concepts from entities
            key_concepts = self._extract_key_concepts(entity_list.entities)

        # Extract topics (simple heuristic for now)
        if analysis_type == "full":
            topics = self._extract_topics_heuristic(content)

        processing_time_ms = int((time.perf_counter() - start_time) * 1000)

        return ModelSemanticAnalysisResult(
            result_id=correlation_id,
            analysis_type=analysis_type,
            analyzed_content=content[:1000],  # Truncate for storage
            content_language=None,  # Language detection not implemented
            semantic_vector=embedding,
            key_concepts=key_concepts,
            entities=entities,
            entity_list=entity_list,
            topics=topics,
            sentiment_score=None,  # Sentiment analysis not implemented
            complexity_score=self._compute_complexity_score(content),
            readability_score=self._compute_readability_score(content),
            coherence_score=None,  # Coherence analysis not implemented
            relevance_score=None,  # Relevance analysis not implemented
            confidence_score=0.9 if embedding else 0.7,
            model_name=self._embedding_provider.model_name,
            model_version=self._config.handler_version,
            processing_time_ms=processing_time_ms,
        )

    # =========================================================================
    # Private Helper Methods
    # =========================================================================

    def _compute_cache_key(self, content: str, model: str | None) -> str:
        """Compute a cache key for content and model.

        Note: Assumes handler is initialized (caller should check).
        """
        assert self._config is not None  # For type checker
        model_name = model or self._config.policy_config.default_embedding_model
        key_input = f"{model_name}:{content}"

        if self._config.policy_config.cache_key_include_model:
            return hashlib.sha256(key_input.encode()).hexdigest()[:32]
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def _extract_entities_heuristic(self, content: str) -> list[ModelSemanticEntity]:
        """Extract entities using simple capitalization-based heuristics.

        dependencies. It identifies capitalized words as potential named entities
        and filters common sentence-starting words to reduce false positives.

        Capabilities:
            - Detects single capitalized words (e.g., "John", "Google", "Paris")
            - Filters common sentence-starting stopwords ("The", "However", etc.)
            - Classifies entities by simple suffix/pattern matching:
                - Organization: words ending in Inc, Corp, LLC, Ltd, etc.
                - Location: words like Street, Avenue, City, etc.
                - Money: words starting with $ or ending with USD/EUR/GBP
                - Percent: words containing %
                - MISC: other capitalized words (default for proper nouns)

        Limitations:
            - Does NOT detect multi-word entities:
                - "New York City" -> extracts "New", "York", "City" separately
                - "United States" -> extracts "United", "States" separately
            - Does NOT detect dates, times, or numeric values:
                - "January 15, 2024" -> only "January" extracted
                - "3:30 PM" -> nothing extracted
            - Acronyms classified as MISC, not their semantic type:
                - "NYC" -> MISC (not LOCATION)
                - "FBI" -> MISC (not ORGANIZATION)
            - No context awareness:
                - Cannot distinguish "Apple" (company) vs "Apple" (fruit)
                - Cannot distinguish "Jordan" (person) vs "Jordan" (country)
            - Misses lowercase named entities:
                - "iPhone", "eBay" -> not detected (starts lowercase)
            - Limited organization detection:
                - "Google" -> MISC (no suffix like Inc/Corp)
                - "Microsoft Corporation" -> only "Microsoft" as MISC, "Corporation" skipped

        Use Cases:
            - Testing and development (deterministic, reproducible results)
            - Offline processing (no external API calls needed)
            - Quick extraction where precision is less critical

        For higher accuracy with multi-word entities, dates, numbers, and
        context-aware classification, use ``entity_extraction_mode=BEST_EFFORT``
        with an LLM provider configured.

        Args:
            content: Text to extract entities from.

        Returns:
            List of extracted entities with type, text, confidence (0.7), and spans.

        Note:
            Assumes handler is initialized (caller should check).
        """
        assert self._config is not None  # For type checker
        entities: list[ModelSemanticEntity] = []
        words = content.split()

        # Track sentence boundaries
        sentence_end_chars = ".!?"

        i = 0
        is_sentence_start = True  # First word is always a sentence start

        for word in words:
            # Find position in original content
            try:
                word_start = content.index(word, i)
                i = word_start + len(word)
            except ValueError:
                continue

            # Strip punctuation for analysis
            clean_word = word.strip(".,!?;:\"'()[]{}").strip()

            # Calculate span for the cleaned word (without punctuation)
            # Find where clean_word starts within word and adjust span accordingly
            clean_offset = word.find(clean_word)
            span_start = word_start + clean_offset
            span_end = span_start + len(clean_word)

            if not clean_word:
                # Check if this word ends a sentence for next iteration
                if any(c in word for c in sentence_end_chars):
                    is_sentence_start = True
                continue

            # Check if this word is at the start of a sentence
            word_is_sentence_start = is_sentence_start

            # Update sentence start tracker for next word
            is_sentence_start = any(c in word for c in sentence_end_chars)

            # Simple heuristic: capitalized words
            if clean_word[0].isupper() and len(clean_word) > 1:
                # Check if this is a sentence-starting stopword
                if word_is_sentence_start and clean_word in SENTENCE_STARTING_STOPWORDS:
                    # Skip common stopwords at sentence start
                    # Note: proper nouns like "The Beatles" - "The" is skipped,
                    # but "Beatles" will be captured on its next iteration
                    continue

                # Determine entity type based on simple patterns
                entity_type = self._classify_entity_heuristic(clean_word)

                if entity_type != EnumSemanticEntityType.UNKNOWN:
                    entities.append(
                        ModelSemanticEntity(
                            entity_type=entity_type,
                            text=clean_word,
                            confidence=self._config.policy_config.heuristic_entity_confidence,
                            span_start=span_start,
                            span_end=span_end,
                        )
                    )

        return entities

    def _classify_entity_heuristic(self, word: str) -> EnumSemanticEntityType:
        """Classify an entity using simple heuristics.

        Args:
            word: The word to classify.

        Returns:
            The entity type classification.
        """
        word_lower = word.lower()

        # Organization indicators
        org_suffixes = {"inc", "corp", "llc", "ltd", "company", "co", "group"}
        if any(word_lower.endswith(suffix) for suffix in org_suffixes):
            return EnumSemanticEntityType.ORGANIZATION

        # Location indicators (very simplified)
        location_words = {
            "street",
            "avenue",
            "road",
            "city",
            "state",
            "country",
            "park",
            "building",
        }
        if word_lower in location_words:
            return EnumSemanticEntityType.LOCATION

        # Money indicators
        if word.startswith("$") or word.endswith(("USD", "EUR", "GBP")):
            return EnumSemanticEntityType.MONEY

        # Percent indicators
        if "%" in word:
            return EnumSemanticEntityType.PERCENT

        # Default to MISC for capitalized words (likely proper nouns)
        if word[0].isupper():
            return EnumSemanticEntityType.MISC

        return EnumSemanticEntityType.UNKNOWN

    async def _extract_entities_llm(
        self,
        content: str,
        correlation_id: UUID | None,
    ) -> list[ModelSemanticEntity]:
        """Extract entities using LLM provider.

        Args:
            content: Text to extract entities from.
            correlation_id: Optional correlation ID.

        Returns:
            List of extracted entities.

        Raises:
            RuntimeError: If LLM provider is not configured.
            Exception: If LLM provider fails (propagated from provider).

        Note:
            Assumes handler is initialized (caller should check).
        """
        assert self._config is not None  # For type checker
        assert self._policy is not None  # For type checker

        if not self._llm_provider:
            raise RuntimeError(
                "LLM provider not configured but LLM entity extraction requested"
            )

        # Build extraction prompt
        prompt = self._build_entity_extraction_prompt(content)
        llm_params = self._policy.get_effective_llm_params()

        # Extract typed values
        temperature = float(llm_params.get("temperature", 0.0) or 0.0)
        seed_val = llm_params.get("seed")
        seed = int(seed_val) if seed_val is not None else None
        timeout = self._config.policy_config.timeout_seconds

        # Define the LLM call to be retried
        async def _do_llm_extract() -> dict[str, object]:
            # Check providers in closure for type checker
            assert self._llm_provider is not None
            assert self._config is not None
            async with asyncio.timeout(timeout):
                return await self._llm_provider.complete_structured(
                    prompt=prompt,
                    output_schema=self._get_entity_extraction_schema(),
                    model=self._config.policy_config.default_llm_model,
                    temperature=temperature,
                    seed=seed,
                    correlation_id=correlation_id,
                    timeout_seconds=timeout,
                )

        try:
            response = await self._execute_with_retry(
                _do_llm_extract, "llm_entity_extraction"
            )
            return self._parse_llm_entity_response(response)

        except Exception:
            logger.exception("LLM entity extraction failed after retries")
            raise

    def _build_entity_extraction_prompt(self, content: str) -> str:
        """Build the prompt for LLM-based entity extraction."""
        return f"""Extract named entities from the following text.
Identify: PERSON, ORGANIZATION, LOCATION, DATE, TIME, MONEY, PERCENT, PRODUCT, EVENT.

Text: {content}

Return a JSON array of entities with: type, text, confidence (0-1), start, end."""

    def _get_entity_extraction_schema(self) -> dict[str, object]:
        """Get JSON schema for entity extraction output."""
        return {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "text": {"type": "string"},
                            "confidence": {"type": "number"},
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                        },
                        "required": ["type", "text", "confidence", "start", "end"],
                    },
                }
            },
            "required": ["entities"],
        }

    def _parse_llm_entity_response(
        self, response: dict[str, object]
    ) -> list[ModelSemanticEntity]:
        """Parse LLM response into entity models."""
        entities: list[ModelSemanticEntity] = []

        # Extract and validate entities list from response
        raw_entities = response.get("entities", [])
        if not isinstance(raw_entities, list):
            return entities

        for item in raw_entities:
            if not isinstance(item, dict):
                continue
            entity_data: dict[str, object] = item

            try:
                type_value = entity_data.get("type", "misc")
                entity_type_str = str(type_value).lower() if type_value else "misc"
                entity_type = EnumSemanticEntityType(entity_type_str)
            except ValueError:
                entity_type = EnumSemanticEntityType.MISC

            text_value = entity_data.get("text", "")
            confidence_value = entity_data.get("confidence", 0.8)
            start_value = entity_data.get("start", 0)
            end_value = entity_data.get("end", 0)

            try:
                entities.append(
                    ModelSemanticEntity(
                        entity_type=entity_type,
                        text=str(text_value) if text_value else "",
                        confidence=min(
                            1.0,
                            max(
                                0.0,
                                float(confidence_value)
                                if isinstance(confidence_value, int | float)
                                else 0.8,
                            ),
                        ),
                        span_start=int(start_value)
                        if isinstance(start_value, int | float)
                        else 0,
                        span_end=int(end_value)
                        if isinstance(end_value, int | float)
                        else 0,
                    )
                )
            except (ValueError, ValidationError) as e:
                logger.warning(
                    "Skipping invalid entity from LLM response: %s (error: %s)",
                    entity_data,
                    e,
                )
                continue

        return entities

    def _extract_key_concepts(self, entities: list[ModelSemanticEntity]) -> list[str]:
        """Extract key concepts from entities."""
        # Use high-confidence named entities as key concepts
        concepts = [
            e.text
            for e in entities
            if e.confidence >= KEY_CONCEPT_CONFIDENCE_THRESHOLD
            and e.entity_type
            in {
                EnumSemanticEntityType.ORGANIZATION,
                EnumSemanticEntityType.PERSON,
                EnumSemanticEntityType.PRODUCT,
                EnumSemanticEntityType.EVENT,
            }
        ]
        return list(dict.fromkeys(concepts))[:10]  # Dedupe and limit

    def _extract_topics_heuristic(self, content: str) -> list[str]:
        """Extract topics using simple heuristics.

        Args:
            content: Text to analyze.

        Returns:
            List of topic strings.
        """
        # Simple word frequency approach
        words = content.lower().split()

        # Filter stop words and short words (using module-level constant)
        filtered_words = [
            w.strip(".,!?;:\"'()[]{}").lower()
            for w in words
            if len(w) > 3 and w.lower() not in TOPIC_EXTRACTION_STOPWORDS
        ]

        # Count frequencies
        freq: dict[str, int] = {}
        for w in filtered_words:
            freq[w] = freq.get(w, 0) + 1

        # Return top words as topics
        sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return [w for w, _ in sorted_words[:5]]

    def _compute_complexity_score(self, content: str) -> float:
        """Compute a simple complexity score.

        Based on average word length and sentence length.

        Args:
            content: Text to analyze.

        Returns:
            Complexity score between 0 and 1.
        """
        words = content.split()
        if not words:
            return 0.0

        avg_word_len = sum(len(w) for w in words) / len(words)

        # Normalize word complexity using module constants
        word_complexity = min(
            1.0,
            max(
                0.0,
                (avg_word_len - COMPLEXITY_WORD_LEN_MIN) / COMPLEXITY_WORD_LEN_RANGE,
            ),
        )

        # Sentence length factor
        sentences = content.count(".") + content.count("!") + content.count("?")
        sentences = max(1, sentences)
        avg_sentence_len = len(words) / sentences

        # Normalize sentence complexity using module constants
        sentence_complexity = min(
            1.0,
            max(
                0.0,
                (avg_sentence_len - COMPLEXITY_SENTENCE_LEN_MIN)
                / COMPLEXITY_SENTENCE_LEN_RANGE,
            ),
        )

        return (word_complexity + sentence_complexity) / 2

    def _compute_readability_score(self, content: str) -> float:
        """Compute a simple readability score.

        Higher score = more readable.

        Args:
            content: Text to analyze.

        Returns:
            Readability score between 0 and 1.
        """
        # Simple inverse of complexity
        complexity = self._compute_complexity_score(content)
        return 1.0 - complexity * 0.5  # Bias toward readable
