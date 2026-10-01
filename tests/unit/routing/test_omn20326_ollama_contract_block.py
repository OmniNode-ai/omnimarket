# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The ollama block of bifrost_delegation.yaml loads and rejects bad shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelOllamaConfig,
)


def _tier(floor: int, model: str = "m:1b", download_gb: float = 1) -> dict[str, object]:
    return {"min_memory_gb": floor, "model": model, "download_gb": download_gb}


def _block(*tiers: dict[str, object]) -> dict[str, object]:
    return {"port": 11434, "chat_path": "/v1/chat/completions", "models": list(tiers)}


@pytest.mark.unit
def test_committed_contract_carries_the_ollama_block() -> None:
    ollama = load_bifrost_delegation_config().ollama
    assert ollama is not None
    assert ollama.port == 11434
    assert ollama.chat_path == "/v1/chat/completions"
    assert [(t.min_memory_gb, t.download_gb) for t in ollama.models] == [
        (16, 5),
        (0, 1),
    ]
    assert [t.model for t in ollama.models] == [
        "qwen2.5-coder:7b",
        "qwen2.5-coder:1.5b",
    ]


@pytest.mark.unit
def test_host_is_not_a_contract_field() -> None:
    with pytest.raises(ValidationError):
        ModelOllamaConfig.model_validate({**_block(_tier(0)), "host": "localhost"})


@pytest.mark.unit
@pytest.mark.parametrize(
    "floors",
    [(0, 16), (16, 16, 0), (8, 8)],
)
def test_models_must_be_strictly_descending(floors: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError, match="strictly descending"):
        ModelOllamaConfig.model_validate(_block(*(_tier(f) for f in floors)))


@pytest.mark.unit
def test_last_entry_must_have_min_memory_zero() -> None:
    with pytest.raises(ValidationError, match="min_memory_gb 0"):
        ModelOllamaConfig.model_validate(_block(_tier(16), _tier(8)))


@pytest.mark.unit
@pytest.mark.parametrize("download_gb", [0, -1])
def test_download_gb_must_be_positive(download_gb: float) -> None:
    with pytest.raises(ValidationError):
        ModelOllamaConfig.model_validate(_block(_tier(0, download_gb=download_gb)))


@pytest.mark.unit
def test_valid_block_validates() -> None:
    cfg = ModelOllamaConfig.model_validate(_block(_tier(16), _tier(0)))
    assert [t.min_memory_gb for t in cfg.models] == [16, 0]
