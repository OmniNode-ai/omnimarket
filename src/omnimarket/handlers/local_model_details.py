# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ask a server the user runs what it is and what it knows (OMN-23413).

models.dev knows nothing about a model on the user's own Ollama or vLLM, so
the server is asked: Ollama's model page gives the context and capabilities;
an OpenAI-compatible model list names its owner (vLLM) and context. How to
ask is data in configs/model_catalog.yaml (local_servers). Any failure leaves
that model's details unknown; nothing here raises to a screen.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from omnimarket.handlers.model_catalog import catalog_config
from omnimarket.models.model_catalog_entry import CapabilityState
from omnimarket.models.model_local_server_details import ModelLocalServerDetails

#: GET a URL within a timeout and return its JSON body; raises on any failure.
GetJson = Callable[[str, float], Any]
#: POST a JSON body to a URL within a timeout and return its JSON; raises on failure.
PostJson = Callable[[str, Mapping[str, Any], float], Any]

#: POST an empty JSON body within a timeout; return the HTTP status, raise when
#: the server cannot be reached.
PostStatus = Callable[[str, float], int]

_UNKNOWN = ModelLocalServerDetails()


def _post_status(url: str, timeout: float) -> int:
    return httpx.post(url, json={}, timeout=timeout).status_code


def speaks_anthropic_messages(
    base: str, post_status: PostStatus | None = None
) -> bool | None:
    """Whether the server has the Anthropic Messages route; None when it cannot say."""
    settings = catalog_config()["local_servers"]
    rules = settings["anthropic_messages"]
    try:
        status = (post_status or _post_status)(
            base.rstrip("/") + str(rules["path"]), float(settings["timeout_seconds"])
        )
    except Exception:  # unreachable: unknown, never "no"
        return None
    if status in {int(s) for s in rules["absent_statuses"]}:
        return False
    return None if status >= 500 else True


def _get_json(url: str, timeout: float) -> Any:
    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _post_json(url: str, body: Mapping[str, Any], timeout: float) -> Any:
    response = httpx.post(url, json=dict(body), timeout=timeout)
    response.raise_for_status()
    return response.json()


def _positive(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _listed(
    base: str, get_json: GetJson, timeout: float
) -> dict[str, Mapping[str, Any]]:
    """The OpenAI-compatible model rows, by id; empty when the server gives none."""
    rules = catalog_config()["local_servers"]["openai_models"]
    try:
        document = get_json(base + str(rules["models_path"]), timeout)
    except Exception:  # unreachable or not OpenAI-compatible: nothing is known
        return {}
    rows = document.get("data") if isinstance(document, dict) else None
    return {
        str(row["id"]): row
        for row in rows or ()
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def _ollama(
    base: str, model: str, post_json: PostJson, timeout: float
) -> ModelLocalServerDetails | None:
    """Ollama's model page, or None when the server is not Ollama."""
    rules = catalog_config()["local_servers"]["ollama"]
    try:
        page = post_json(base + str(rules["show_path"]), {"model": model}, timeout)
    except Exception:  # not Ollama, or the model is gone
        return None
    if not isinstance(page, dict) or not (
        "model_info" in page or "capabilities" in page
    ):
        return None
    info = page.get("model_info")
    info = info if isinstance(info, dict) else {}
    architecture = info.get(str(rules["architecture_key"]))
    context = (
        _positive(info.get(f"{architecture}{rules['context_suffix']}"))
        if isinstance(architecture, str)
        else None
    )
    stated = page.get("capabilities")
    capabilities: dict[str, CapabilityState] = {}
    if isinstance(stated, list):
        for name, label in dict(rules["capabilities"]).items():
            capabilities[str(label)] = "yes" if name in stated else "no"
    return ModelLocalServerDetails(
        server=str(rules["server"]),
        context_tokens=context,
        capabilities=capabilities,
    )


def _from_list(row: Mapping[str, Any] | None) -> ModelLocalServerDetails:
    rules = catalog_config()["local_servers"]["openai_models"]
    if row is None:
        return _UNKNOWN
    owner = row.get("owned_by")
    server = dict(rules["owners"]).get(owner) if isinstance(owner, str) else None
    return ModelLocalServerDetails(
        server=str(server) if server else None,
        context_tokens=_positive(row.get(str(rules["context_field"]))),
    )


def read_local_details(
    base: str,
    models: Iterable[str],
    *,
    get_json: GetJson | None = None,
    post_json: PostJson | None = None,
    post_status: PostStatus | None = None,
) -> dict[str, ModelLocalServerDetails]:
    """Each model's details from the server at ``base``; unknown where it is silent.

    The model list is read once. A model whose owner the list already names
    (vLLM) is not asked again; every other one is asked as Ollama, in parallel.
    """
    settings = catalog_config()["local_servers"]
    timeout = float(settings["timeout_seconds"])
    wanted = list(dict.fromkeys(models))
    base = base.rstrip("/")
    listed = _listed(base, get_json or _get_json, timeout)
    found = {model: _from_list(listed.get(model)) for model in wanted}
    ask = [model for model in wanted if found[model].server is None]
    if ask:
        post = post_json or _post_json
        with ThreadPoolExecutor(max_workers=int(settings["max_parallel"])) as pool:
            pages = pool.map(lambda model: _ollama(base, model, post, timeout), ask)
            for model, page in zip(ask, pages, strict=True):
                if page is not None:
                    found[model] = page
    native = speaks_anthropic_messages(base, post_status) if wanted else None
    return {
        model: said.model_copy(update={"native_messages": native})
        for model, said in found.items()
    }


__all__: list[str] = [
    "GetJson",
    "PostJson",
    "PostStatus",
    "read_local_details",
    "speaks_anthropic_messages",
]
