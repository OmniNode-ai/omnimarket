# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C14 resolve step reads a llama.cpp listing without a KeyError (OMN-26043).

The second lab GPU host serves a llama.cpp ``/v1/models`` listing: no ``max_model_len`` on the entry, the serving window in
``meta.n_ctx`` (``meta.n_ctx_train`` is the training window and is never the serving one). The check read
``entry["max_model_len"]`` and raised ``KeyError`` every hour once the chain fell from a vLLM server to this one. The fixture
is the listing that host answered on 2026-11-24 (one GET of /v1/models; the file holds no address, the address sits in this test).
"""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    reading_from_models_body,
    resolve_chain,
    window_of,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)

FIXTURE = Path(__file__).parent / "fixtures" / "llamacpp_models_listing_pc2.json"
LLAMACPP_HOST = "http://127.0.0.12:8000"


def _body() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_listing_has_no_max_model_len_and_a_serving_window_in_meta() -> None:
    entry = _body()["data"][0]  # type: ignore[index]
    assert "max_model_len" not in entry
    assert entry["meta"]["n_ctx"] != entry["meta"]["n_ctx_train"]


def test_the_window_is_meta_n_ctx_not_the_training_window() -> None:
    reading = reading_from_models_body(LLAMACPP_HOST, _body())
    assert reading.max_model_len is None
    assert window_of(reading) == 131072


def test_the_chain_chooses_the_llamacpp_responder_from_the_listing() -> None:
    resolution = resolve_chain(
        (ModelDelegationCheckResponder(tier="local", base_url=LLAMACPP_HOST),),
        lambda base: reading_from_models_body(base, _body()),
    )
    assert resolution.is_local
    assert resolution.base_url == LLAMACPP_HOST
    assert resolution.served_model == "Qwen3.8-27B"
    assert resolution.window == 131072
    assert [a.outcome for a in resolution.attempts] == ["chosen"]
