# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPipelineCache — filesystem-cached test + golden-chain generation pipeline.

Cache layout (all paths relative to cache_root):
  {contract_hash}/{generator_hash}/{profile_hash}/test_generation_result.json
  {contract_hash}/{generator_hash}/{profile_hash}/chain_generation_result.json

cache_hit is advisory metadata only. A hit and a miss for identical inputs
produce byte-identical compute output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Literal

from omnimarket.nodes.node_golden_chain_generator_compute.handlers.handler_golden_chain_generator import (
    HandlerGoldenChainGenerator,
)
from omnimarket.nodes.node_golden_chain_generator_compute.models.model_golden_chain_generation_request import (
    ModelGoldenChainGenerationRequest,
)
from omnimarket.nodes.node_golden_chain_generator_compute.models.model_golden_chain_generation_result import (
    ModelGoldenChainGenerationResult,
)
from omnimarket.nodes.node_pipeline_cache_effect.models.model_pipeline_cache_request import (
    ModelPipelineCacheRequest,
)
from omnimarket.nodes.node_pipeline_cache_effect.models.model_pipeline_cache_result import (
    ModelPipelineCacheResult,
)
from omnimarket.nodes.node_test_generator_compute.handlers.handler_test_generator import (
    HandlerTestGenerator,
)
from omnimarket.nodes.node_test_generator_compute.models.model_test_generation_request import (
    ModelTestGenerationRequest,
)
from omnimarket.nodes.node_test_generator_compute.models.model_test_generation_result import (
    ModelTestGenerationResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_ROOT = Path(".onex_state") / "pipeline_cache"

_TEST_RESULT_FILENAME = "test_generation_result.json"
_CHAIN_RESULT_FILENAME = "chain_generation_result.json"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cache_key(contract_hash: str, generator_hash: str, profile_hash: str) -> str:
    return f"{contract_hash}/{generator_hash}/{profile_hash}"


def _resolve_cache_root(override: str | None) -> Path:
    if override is not None:
        return Path(override)
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        return Path(omni_home) / ".onex_state" / "pipeline_cache"
    return _DEFAULT_CACHE_ROOT


def _cache_dir(root: Path, key: str) -> Path:
    return root / key


def _read_cached(
    cache_dir: Path,
    model_class: type[ModelTestGenerationResult]
    | type[ModelGoldenChainGenerationResult],
    filename: str,
) -> ModelTestGenerationResult | ModelGoldenChainGenerationResult | None:
    path = cache_dir / filename
    if not path.exists():
        return None
    try:
        return model_class.model_validate_json(path.read_text())
    except Exception:
        logger.warning("Corrupt cache entry at %s — treating as miss", path)
        return None


def _write_cached(
    cache_dir: Path,
    result: ModelTestGenerationResult | ModelGoldenChainGenerationResult,
    filename: str,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / filename).write_text(
        json.dumps(result.model_dump(mode="json"), sort_keys=True, indent=2)
    )


class HandlerPipelineCache:
    """Wire test-generator and golden-chain-generator with filesystem cache.

    Inject test_generator or chain_generator in tests to avoid touching real
    compute handlers. Inject fs_root to redirect cache I/O to a temp directory.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        test_generator: HandlerTestGenerator | None = None,
        chain_generator: HandlerGoldenChainGenerator | None = None,
    ) -> None:
        self._test_gen = test_generator or HandlerTestGenerator()
        self._chain_gen = chain_generator or HandlerGoldenChainGenerator()

    def handle(self, request: ModelPipelineCacheRequest) -> ModelPipelineCacheResult:
        from omnimarket.nodes.node_test_generator_compute.handlers.handler_test_generator import (
            _contract_hash,
        )

        contract_hash = _contract_hash(request.contract)
        generator_hash = _sha256(request.generator_version)
        profile_hash = _sha256(request.generation_profile_hash)
        key = _cache_key(contract_hash, generator_hash, profile_hash)

        root = _resolve_cache_root(request.cache_root)
        entry_dir = _cache_dir(root, key)

        cached_test = _read_cached(
            entry_dir, ModelTestGenerationResult, _TEST_RESULT_FILENAME
        )
        cached_chain = _read_cached(
            entry_dir, ModelGoldenChainGenerationResult, _CHAIN_RESULT_FILENAME
        )

        if cached_test is not None and cached_chain is not None:
            logger.debug("pipeline cache hit: key=%s", key)
            return ModelPipelineCacheResult(
                cache_hit=True,
                cache_key=key,
                test_generation_result=cached_test,
                chain_generation_result=cached_chain,
            )

        logger.debug("pipeline cache miss: key=%s", key)

        test_result = self._test_gen.handle(
            ModelTestGenerationRequest(
                contract=request.contract,
                generator_version=request.generator_version,
                generation_profile_hash=request.generation_profile_hash,
            )
        )

        generated_test_hash: str | None = None
        if test_result.generated_files:
            generated_test_hash = test_result.generated_files[0].content_sha256

        chain_result = self._chain_gen.handle(
            ModelGoldenChainGenerationRequest(
                contract=request.contract,
                generated_test_hash=generated_test_hash,
                generator_version=request.generator_version,
            )
        )

        _write_cached(entry_dir, test_result, _TEST_RESULT_FILENAME)
        _write_cached(entry_dir, chain_result, _CHAIN_RESULT_FILENAME)

        return ModelPipelineCacheResult(
            cache_hit=False,
            cache_key=key,
            test_generation_result=test_result,
            chain_generation_result=chain_result,
        )


__all__ = ["HandlerPipelineCache"]
