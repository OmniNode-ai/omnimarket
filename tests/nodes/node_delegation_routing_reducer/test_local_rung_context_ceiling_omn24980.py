# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24980: a summarise prompt above 8192 tokens is still offered to the local rung.

Recorded on the dev lane, 2026-11-16: a 44290-character summarisation prompt (11072 tokens by
the router's own estimate) delegated with ``--task-type summarization`` was answered by
``openrouter-north-mini-code`` (run 8a9b604a-d4b4-43b6-8231-f0fc61b8074f, tier cheap_frontier), while
a short one was answered by ``local-heavy-reasoning`` (run 8f99aa94-0b83-44b5-8bd5-7771ebdac81c).
The router drops a rung whose ``max_context_tokens`` is below the prompt, and the packaged ladder
declared ``max_context_tokens: 8192`` on ``local-heavy-reasoning`` (and 65536 on ``local-coder``)
while the endpoint behind both serves ``max_model_len`` 131072. The size a local rung is offered a
class is the code budget overlay's ``input_ceilings``, not the packaged window.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.routing.routing_tiers_path import resolve_routing_tiers_path
from omnimarket.models.delegation.wire import parse_delegation_config_yaml
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    BifrostBackendRef,
    _estimate_prompt_tokens,
    _select_model_for_task,
)

pytestmark = pytest.mark.unit

#: max_model_len the first lab model host's vLLM endpoint reports on its model list.
_SERVED_WINDOW_TOKENS = 131072
_RECORDED_PROMPT_CHARS = 44290
_LOCAL_BACKENDS = ("local-coder", "local-heavy-reasoning")


def _local_models():  # type: ignore[no-untyped-def]
    config = parse_delegation_config_yaml(
        resolve_routing_tiers_path().read_text(encoding="utf-8")
    )
    (tier,) = [t for t in config.tiers if t.name == "local"]
    return tier.models


def _routable_backends() -> dict[str, BifrostBackendRef]:
    return {
        ref: BifrostBackendRef(
            endpoint_url="http://127.0.0.1:18742/v1/chat/completions",  # url-authority-ok: test loopback
            model_name="Qwen3.8-27B",
            timeout_ms=300000,
            max_tokens=65536,
        )
        for ref in _LOCAL_BACKENDS
    }


def test_recorded_summarise_prompt_is_over_the_old_ceiling() -> None:
    """Control: the recorded prompt really is above 8192 by the router's estimator."""
    assert _estimate_prompt_tokens("x" * _RECORDED_PROMPT_CHARS) > 8192


def test_summarise_prompt_above_8192_tokens_is_offered_to_the_local_rung() -> None:
    selected = _select_model_for_task(
        _local_models(),
        "summarization",
        _estimate_prompt_tokens("x" * _RECORDED_PROMPT_CHARS),
        _routable_backends(),
    )
    assert selected is not None, (
        "the local tier offered no rung for a recorded 11072-token summarise prompt, "
        "so it fell to a hosted model"
    )
    assert selected.backend_ref == "local-heavy-reasoning"


@pytest.mark.parametrize("backend_ref", _LOCAL_BACKENDS)
def test_local_rung_window_is_the_served_window(backend_ref: str) -> None:
    (model,) = [m for m in _local_models() if m.backend_ref == backend_ref]
    assert model.max_context_tokens == _SERVED_WINDOW_TOKENS, (
        f"{backend_ref} declares {model.max_context_tokens}, the endpoint serves "
        f"{_SERVED_WINDOW_TOKENS}"
    )
