# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a server the user runs says about its models (OMN-23413).

Failure modes:
  L1. An Ollama model is shown as vLLM because both are stored as models by URL.
  L2. The context or capabilities the server states are not shown.
  L3. A server that is down, or answers something else, breaks the screen or
      makes up a value instead of leaving it unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from omnimarket.handlers.local_model_details import read_local_details

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_messages_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches a real server: the Messages probe is unknown unless a test says."""
    from omnimarket.handlers import local_model_details

    def down(url: str, timeout: float) -> int:
        raise OSError("no network in unit tests")

    monkeypatch.setattr(local_model_details, "_post_status", down)


_BASE = "http://server.test:11434"
_OLLAMA_PAGE = {
    "capabilities": ["completion", "tools", "thinking"],
    "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
}


def _no_list(url: str, timeout: float) -> Any:
    raise OSError("no OpenAI-compatible list here")


def test_an_ollama_model_is_named_ollama_with_its_context_and_capabilities() -> None:
    """L1, L2."""
    asked: list[tuple[str, Mapping[str, Any]]] = []

    def show(url: str, body: Mapping[str, Any], timeout: float) -> Any:
        asked.append((url, body))
        return _OLLAMA_PAGE

    found = read_local_details(
        _BASE, ["saluki:latest"], get_json=_no_list, post_json=show
    )
    said = found["saluki:latest"]
    assert said.server == "Ollama"
    assert said.context_tokens == 40960
    assert said.capabilities == {"Tools": "yes", "Reason": "yes", "Vision": "no"}
    assert asked == [(_BASE + "/api/show", {"model": "saluki:latest"})]


def test_a_vllm_model_is_named_by_its_owner_and_not_asked_as_ollama() -> None:
    def listing(url: str, timeout: float) -> Any:
        assert url == _BASE + "/v1/models"
        return {
            "data": [{"id": "Qwen3.8-27B", "owned_by": "vllm", "max_model_len": 131072}]
        }

    def must_not_post(url: str, body: Mapping[str, Any], timeout: float) -> Any:
        raise AssertionError("a model the list named was asked again")

    said = read_local_details(
        _BASE, ["Qwen3.8-27B"], get_json=listing, post_json=must_not_post
    )["Qwen3.8-27B"]
    assert said.server == "vLLM"
    assert said.context_tokens == 131072
    assert said.capabilities == {}


def test_ollamas_own_openai_list_does_not_hide_its_model_page() -> None:
    """Ollama also serves /v1/models (owner "library"): the page still decides."""

    def listing(url: str, timeout: float) -> Any:
        return {"data": [{"id": "saluki:latest", "owned_by": "library"}]}

    said = read_local_details(
        _BASE,
        ["saluki:latest"],
        get_json=listing,
        post_json=lambda _url, _body, _timeout: _OLLAMA_PAGE,
    )["saluki:latest"]
    assert said.server == "Ollama"


def test_a_server_that_is_down_leaves_every_detail_unknown() -> None:
    """L3."""

    def down(url: str, body: Mapping[str, Any], timeout: float) -> Any:
        raise OSError("connection refused")

    said = read_local_details(_BASE, ["m"], get_json=_no_list, post_json=down)["m"]
    assert (said.server, said.context_tokens, said.capabilities) == (None, None, {})


def test_an_answer_of_the_wrong_shape_is_not_read_as_details() -> None:
    said = read_local_details(
        _BASE,
        ["m"],
        get_json=lambda _url, _timeout: {"data": "not a list"},
        post_json=lambda _url, _body, _timeout: ["not", "a", "page"],
    )["m"]
    assert (said.server, said.context_tokens) == (None, None)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, False), (405, False), (400, True), (422, True), (503, None)],
)
def test_the_messages_route_is_asked_once_per_server_and_404_or_405_mean_no(
    status: int, expected: bool | None
) -> None:
    """T5: one empty request per server, whatever the number of models."""
    asked: list[str] = []

    def post_status(url: str, timeout: float) -> int:
        asked.append(url)
        return status

    found = read_local_details(
        _BASE,
        ["a", "b", "c"],
        get_json=_no_list,
        post_json=lambda _url, _body, _timeout: _OLLAMA_PAGE,
        post_status=post_status,
    )
    assert asked == [_BASE + "/v1/messages"]
    assert {said.native_messages for said in found.values()} == {expected}


# L4 (OMN-23413): a llama.cpp model showed context "-" and no capabilities,
# though its list states the context it serves (meta.n_ctx) and its /props page
# states tools, reasoning and vision. n_ctx_train (training) must not be read.
_LLAMA_LIST = {
    "data": [
        {
            "id": "Qwen3.8-27B",
            "owned_by": "llamacpp",
            "meta": {"n_ctx": 131072, "n_ctx_train": 262144},
        }
    ]
}
_LLAMA_PROPS = {
    "chat_template_caps": {
        "supports_tools": True,
        "supports_reasoning_effort": True,
    },
    "modalities": {"vision": False},
}


def test_a_llama_cpp_model_shows_the_context_it_serves_and_its_props() -> None:
    """L4."""
    pages = {"/v1/models": _LLAMA_LIST, "/props": _LLAMA_PROPS}
    read: list[str] = []

    def get(url: str, timeout: float) -> Any:
        read.append(url)
        return pages[url.removeprefix(_BASE)]

    def not_ollama(url: str, body: Mapping[str, Any], timeout: float) -> Any:
        raise AssertionError("a model the list names is not asked as Ollama")

    said = read_local_details(
        _BASE, ["Qwen3.8-27B"], get_json=get, post_json=not_ollama
    )["Qwen3.8-27B"]
    assert said.server == "llama.cpp"
    assert said.context_tokens == 131072  # served, not the 262144 trained
    assert said.capabilities == {"Tools": "yes", "Reason": "yes", "Vision": "no"}
    assert read == [f"{_BASE}/v1/models", f"{_BASE}/props"]  # each page once


def test_a_llama_cpp_props_page_that_is_down_leaves_capabilities_unknown() -> None:
    """L3 for the props page: the context still shows; capabilities stay unknown."""

    def get(url: str, timeout: float) -> Any:
        if url.endswith("/props"):
            raise OSError("down")
        return _LLAMA_LIST

    said = read_local_details(_BASE, ["Qwen3.8-27B"], get_json=get)["Qwen3.8-27B"]
    assert said.context_tokens == 131072
    assert said.capabilities == {}
