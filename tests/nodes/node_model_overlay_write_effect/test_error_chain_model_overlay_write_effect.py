# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: what the model overlay write refuses, and that it writes nothing (OMN-20330)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_model_overlay_write_effect.handlers import (
    handler_model_overlay_write as module,
)
from omnimarket.nodes.node_model_overlay_write_effect.handlers.handler_model_overlay_write import (
    HandlerModelOverlayWrite,
)
from omnimarket.nodes.node_model_overlay_write_effect.models.model_model_overlay_write_request import (
    ModelModelOverlayWriteRequest,
)
from omnimarket.nodes.node_model_overlay_write_effect.protocols.protocol_overlay_file_store_backends import (
    InMemoryOverlayFileStore,
)

pytestmark = pytest.mark.unit

_TARGET = Path("/overlay/o.yaml")


def _request(mode: str = "native", **fields: object) -> ModelModelOverlayWriteRequest:
    base: dict[str, object] = {
        "mode": mode,
        "source": "ollama",
        "memory_gb": 16,
        "target_path": _TARGET,
    }
    return ModelModelOverlayWriteRequest.model_validate({**base, **fields})


def test_a_file_the_developer_wrote_is_never_overwritten() -> None:
    store = InMemoryOverlayFileStore()
    store.write_text(
        _TARGET, "backends:\n  - backend_id: local-coder\n    model_name: mine\n"
    )
    result = HandlerModelOverlayWrite(store).handle(_request())
    assert result.outcome == "refused"
    assert "not written by onboarding" in (result.reason or "")
    assert "mine" in (store.read_text(_TARGET) or "")


def test_the_lab_stack_own_key_mode_is_refused_and_writes_nothing() -> None:
    store = InMemoryOverlayFileStore()
    result = HandlerModelOverlayWrite(store).handle(_request("lab_stack"))
    assert result.outcome == "refused"
    assert "out of the MVP" in (result.reason or "")
    assert store.files == {}


def test_injected_break_a_key_reference_in_the_overlay_is_refused_before_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The named break for AC3: the renderer is made to emit a secret_ref."""
    store = InMemoryOverlayFileStore()
    handler = HandlerModelOverlayWrite(store)
    real = HandlerModelOverlayWrite._render

    def leaking(self: HandlerModelOverlayWrite, mode: str, model: Any) -> str:
        return real(self, mode, model) + "    secret_ref: llm.openrouter.api_key\n"

    monkeypatch.setattr(HandlerModelOverlayWrite, "_render", leaking)
    result = handler.handle(_request())
    assert result.outcome == "refused"
    assert "secret_ref" in (result.reason or "")
    assert store.files == {}


def test_without_the_guard_the_same_break_would_write_the_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: with the forbidden words emptied, the break reaches the store."""
    store = InMemoryOverlayFileStore()
    config = {**module.node_config(), "forbidden_text": []}
    real = HandlerModelOverlayWrite._render
    monkeypatch.setattr(
        HandlerModelOverlayWrite,
        "_render",
        lambda self, mode, model: real(self, mode, model) + "    secret_ref: x\n",
    )
    HandlerModelOverlayWrite(store, config=config).handle(_request())
    assert "secret_ref" in (store.read_text(_TARGET) or "")


def test_a_url_with_userinfo_is_refused() -> None:
    result = HandlerModelOverlayWrite().handle(
        _request(
            source="url",
            url="http://user:pw@localhost:8000",
            served_model_id="m",
            parameter_count="7B",
            context_window=8192,
        )
    )
    assert result.outcome == "refused"
    assert "no user, password" in (result.reason or "")
    assert "pw" not in (result.reason or "")


def test_a_ceiling_above_the_window_is_refused() -> None:
    result = HandlerModelOverlayWrite().handle(
        _request(
            source="url",
            url="http://localhost:8000",
            served_model_id="m",
            parameter_count="7B",
            context_window=4096,
            max_tokens=8192,
        )
    )
    assert result.outcome == "refused"


def test_a_missing_ollama_block_is_refused_not_defaulted() -> None:
    result = HandlerModelOverlayWrite(ollama_loader=lambda: None).handle(_request())
    assert result.outcome == "refused"
    assert "no ollama block" in (result.reason or "")


def test_a_url_source_without_its_model_is_a_validation_error() -> None:
    with pytest.raises(ValidationError, match="served_model_id"):
        ModelModelOverlayWriteRequest(mode="native", source="url", url="http://h:1")


def test_an_ollama_model_the_block_does_not_declare_is_refused() -> None:
    result = HandlerModelOverlayWrite().handle(
        _request(served_model_id="not-in-the-block:1b")
    )
    assert result.outcome == "refused"
    assert "not a model the ollama block declares" in (result.reason or "")
