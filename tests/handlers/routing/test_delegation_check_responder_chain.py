# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation check responder chain (OMN-26043): the window is max_model_len else meta.n_ctx, a windowless
responder is skipped, the least busy local responder carries the task, and a chain with no usable local responder
advances to the cloud tiers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelResponderReading,
    reading_from_models_body,
    resolve_chain,
    window_of,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
    ModelDelegationCheckRespondersOverlay,
)

INKLING = "http://127.0.0.11:8111"
QWEN = "http://127.0.0.12:8000"


def _chain() -> tuple[ModelDelegationCheckResponder, ...]:
    return (
        ModelDelegationCheckResponder(tier="local", base_url=INKLING),
        ModelDelegationCheckResponder(tier="local", base_url=QWEN),
        ModelDelegationCheckResponder(tier="openrouter"),
        ModelDelegationCheckResponder(tier="claude_api"),
    )


VLLM_BODY = {"data": [{"id": "inkling-2-mini", "max_model_len": 65536}]}
LLAMACPP_BODY = {"data": [{"id": "qwen3.8-27b", "meta": {"n_ctx": 131072}}]}
WINDOWLESS_BODY = {"data": [{"id": "mystery"}]}


def _reader(bodies: dict[str, dict | None], busy: dict[str, int] | None = None):
    def read(base: str) -> ModelResponderReading:
        return reading_from_models_body(
            base, bodies.get(base), busy=(busy or {}).get(base)
        )

    return read


def test_llamacpp_window_is_n_ctx_when_max_model_len_is_absent() -> None:
    # The failure C14 read every hour: a llama.cpp server has no max_model_len (KeyError in the old resolve step).
    reading = reading_from_models_body(QWEN, LLAMACPP_BODY)
    assert reading.max_model_len is None
    assert window_of(reading) == 131072


def test_max_model_len_wins_over_n_ctx() -> None:
    body = {"data": [{"id": "m", "max_model_len": 4096, "meta": {"n_ctx": 8192}}]}
    assert window_of(reading_from_models_body(QWEN, body)) == 4096


def test_a_responder_declaring_no_window_is_skipped_with_its_reason() -> None:
    resolution = resolve_chain(
        _chain(), _reader({INKLING: WINDOWLESS_BODY, QWEN: LLAMACPP_BODY})
    )
    assert resolution.tier == "local"
    assert resolution.base_url == QWEN
    assert resolution.window == 131072
    by_base = {a.base_url: a.outcome for a in resolution.attempts if a.base_url}
    assert by_base[INKLING] == "no-window"
    assert by_base[QWEN] == "chosen"


def test_declared_order_decides_when_no_responder_reports_load() -> None:
    resolution = resolve_chain(
        _chain(), _reader({INKLING: VLLM_BODY, QWEN: LLAMACPP_BODY})
    )
    assert resolution.base_url == INKLING
    assert resolution.served_model == "inkling-2-mini"


def test_least_busy_local_responder_carries_the_task() -> None:
    resolution = resolve_chain(
        _chain(),
        _reader({INKLING: VLLM_BODY, QWEN: LLAMACPP_BODY}, busy={INKLING: 5, QWEN: 1}),
    )
    assert resolution.base_url == QWEN
    outcomes = {a.base_url: a.outcome for a in resolution.attempts if a.base_url}
    assert outcomes[INKLING] == "usable-busier"


def test_a_responder_that_reports_load_outranks_one_that_does_not() -> None:
    resolution = resolve_chain(
        _chain(), _reader({INKLING: VLLM_BODY, QWEN: LLAMACPP_BODY}, busy={QWEN: 3})
    )
    assert resolution.base_url == QWEN


def test_no_usable_local_responder_advances_to_openrouter_then_claude_api() -> None:
    resolution = resolve_chain(_chain(), _reader({INKLING: None, QWEN: None}))
    assert resolution.tier == "openrouter"
    assert resolution.base_url is None
    assert [a.outcome for a in resolution.attempts] == [
        "no-answer",
        "no-answer",
        "advanced-to-tier",
        "advanced-to-tier",
    ]
    assert [a.tier for a in resolution.attempts][-2:] == ["openrouter", "claude_api"]


def test_an_answer_with_no_served_model_is_no_model() -> None:
    resolution = resolve_chain(_chain(), _reader({INKLING: {"data": []}, QWEN: None}))
    assert resolution.attempts[0].outcome == "no-model"


def test_the_row_must_try_local_then_openrouter_then_claude_api() -> None:
    ok = {
        "schema_version": 1,
        "responders": [
            {"tier": "local", "base_url": INKLING},
            {"tier": "local", "base_url": QWEN},
            {"tier": "openrouter"},
            {"tier": "claude_api"},
        ],
    }
    assert len(ModelDelegationCheckRespondersOverlay.model_validate(ok).responders) == 4
    swapped = {**ok, "responders": [ok["responders"][2], ok["responders"][0]]}
    with pytest.raises(ValidationError, match="local responders first"):
        ModelDelegationCheckRespondersOverlay.model_validate(swapped)


@pytest.mark.parametrize(
    "responder",
    [
        {"tier": "local", "base_url": "http://8.8.8.8:8000"},
        {"tier": "local", "base_url": "http://lab-host:8000"},
        {"tier": "local"},
        {"tier": "openrouter", "base_url": INKLING},
    ],
)
def test_a_malformed_responder_is_refused(responder: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ModelDelegationCheckResponder.model_validate(responder)
