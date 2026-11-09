# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write the developer model overlay for the mode being set up (OMN-20330).

Native ``onex`` reads a ``backends`` overlay merged over the base contract; the
local compose stack's renderer reads a ``bifrost_lane_overlay.v3`` lane overlay.
Both rebind the base contract's local rungs to the developer's model. Every
setting (paths, hosts, rung ids, the forbidden key words) is this node's
contract config; the model, window and ceiling for Ollama are the base
contract's ``ollama`` block. Nothing here reads or writes a key.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_ollama_config,
)
from omnimarket.models.delegation.model_ollama_config import (
    ModelOllamaConfig,
    ModelOllamaModelTier,
)
from omnimarket.nodes.node_model_overlay_write_effect.handlers.handler_overlay_file_store import (
    InMemoryOverlayFileStore,
)
from omnimarket.nodes.node_model_overlay_write_effect.models.model_model_overlay_write_request import (
    ModelModelOverlayWriteRequest,
)
from omnimarket.nodes.node_model_overlay_write_effect.models.model_model_overlay_write_result import (
    ModelModelOverlayWriteResult,
)
from omnimarket.nodes.node_model_overlay_write_effect.protocols.protocol_overlay_file_store import (
    ProtocolOverlayFileStore,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_CHAT_PATH = "/v1/chat/completions"

OllamaLoader = Callable[[], ModelOllamaConfig | None]


def node_config() -> dict[str, Any]:
    """The ``config`` block of this node's contract."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")) or {}
    config = contract.get("config")
    if not isinstance(config, dict):
        raise ValueError(f"{_CONTRACT} declares no config block")
    return config


class _Model:
    """The one model both overlay shapes state."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        served_model_id: str,
        parameter_count: str,
        context_window: int,
        max_tokens: int,
        timeout_ms: int,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.served_model_id = served_model_id
        self.parameter_count = parameter_count
        self.context_window = context_window
        self.max_tokens = max_tokens
        self.timeout_ms = timeout_ms


class HandlerModelOverlayWrite:
    """``handle(request)`` writes the mode's overlay through the injected store."""

    def __init__(
        self,
        store: ProtocolOverlayFileStore | None = None,
        *,
        config: dict[str, Any] | None = None,
        ollama_loader: OllamaLoader = load_ollama_config,
    ) -> None:
        self._store: ProtocolOverlayFileStore = store or InMemoryOverlayFileStore()
        self._config = config if config is not None else node_config()
        self._ollama_loader = ollama_loader

    def handle(
        self, request: ModelModelOverlayWriteRequest
    ) -> ModelModelOverlayWriteResult:
        mode = request.mode
        if mode == "lab_stack":
            return self._refused(
                request,
                None,
                "the lab stack with your own key is out of the MVP (operator ruling "
                "2026-10-01, Q4); the lab stack serves the lab's models and needs no "
                "overlay from you. Nothing was written",
            )
        mode_config = self._config["modes"][mode]
        path = request.target_path or Path(str(mode_config["target_path"])).expanduser()
        try:
            model = self._model(request, mode_config)
        except ValueError as refusal:
            return self._refused(request, path, str(refusal))

        marker = request.marker_line or str(self._config["marker_line"])
        text = f"{marker}\n{self._render(mode, model)}"
        words = [w for w in self._config["forbidden_text"] if w in text]
        if words:
            return self._refused(
                request,
                path,
                f"the overlay would hold {', '.join(words)}, and no key or key "
                "reference belongs in a developer model overlay. Nothing was written",
            )
        existing = self._store.read_text(path)
        if existing is not None:
            if existing == text:
                return self._result(request, path, model, "unchanged")
            first = existing.splitlines()[0] if existing.strip() else ""
            ours = [
                marker,
                self._config["marker_line"],
                *self._config["replaceable_first_lines"],
            ]
            if not any(first.startswith(line) for line in ours):
                return self._refused(
                    request,
                    path,
                    f"{path} was not written by onboarding, so it is left as it is. "
                    "Move it aside, or edit it to name your model, then run this again",
                )
        self._store.write_text(path, text)
        return self._result(request, path, model, "written")

    # -- the model ---------------------------------------------------------

    def _model(
        self, request: ModelModelOverlayWriteRequest, mode_config: dict[str, Any]
    ) -> _Model:
        timeout_ms = request.timeout_ms or int(self._config["default_timeout_ms"])
        if request.source == "ollama":
            try:
                ollama = self._ollama_loader()
            except ValueError as error:
                raise ValueError(
                    f"the base contract's ollama block is unreadable: {error}"
                ) from None
            if ollama is None:
                raise ValueError("the base contract declares no ollama block")
            memory_gb = request.memory_gb or 0
            tier = _tier_for(ollama, memory_gb, request.served_model_id)
            return _Model(
                endpoint_url=f"http://{mode_config['host']}:{ollama.port}{ollama.chat_path}",
                served_model_id=tier.model,
                parameter_count=tier.parameter_count,
                context_window=tier.context_window,
                max_tokens=request.max_tokens or tier.max_tokens,
                timeout_ms=timeout_ms,
            )
        if request.url is None or request.context_window is None:
            raise ValueError("source 'url' needs url and context_window")
        max_tokens = request.max_tokens or min(
            int(self._config["default_max_tokens"]), request.context_window
        )
        if max_tokens > request.context_window:
            raise ValueError("max_tokens must not exceed context_window")
        return _Model(
            endpoint_url=self._endpoint(request.url, mode_config),
            served_model_id=str(request.served_model_id),
            parameter_count=str(request.parameter_count),
            context_window=request.context_window,
            max_tokens=max_tokens,
            timeout_ms=timeout_ms,
        )

    @staticmethod
    def _endpoint(url: str, mode_config: dict[str, Any]) -> str:
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{url!r} is not an http(s) URL with a host")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("the URL must carry no user, password, query or fragment")
        host = parts.hostname
        if host in mode_config.get("loopback_hosts", []):
            host = str(mode_config["host"])
        netloc = f"{host}:{parts.port}" if parts.port else host
        path = parts.path.rstrip("/")
        if path.endswith(_CHAT_PATH):
            path = path[: -len(_CHAT_PATH)]
        return urlunsplit((parts.scheme, netloc, f"{path}{_CHAT_PATH}", "", ""))

    # -- the two shapes ----------------------------------------------------

    def _render(self, mode: str, model: _Model) -> str:
        if mode == "native":
            document: dict[str, Any] = {
                "backends": [
                    {
                        "backend_id": backend_id,
                        "endpoint_url": model.endpoint_url,
                        "model_name": model.served_model_id,
                        "max_tokens": model.max_tokens,
                        "timeout_ms": model.timeout_ms,
                    }
                    for backend_id in self._config["rebound_backends"]
                ]
            }
        else:
            lane = self._config["lane_overlay"]
            document = {
                "schema_version": lane["schema_version"],
                "lane": lane["lane"],
                "locale": lane["locale"],
                "backends": [
                    {
                        "backend_id": backend_id,
                        "endpoint_url": model.endpoint_url,
                        "served_model_id": model.served_model_id,
                        "parameter_count": model.parameter_count,
                        "context_window": model.context_window,
                        "max_tokens": model.max_tokens,
                        "timeout_ms": model.timeout_ms,
                    }
                    for backend_id in self._config["rebound_backends"]
                ],
            }
        return yaml.safe_dump(document, sort_keys=False)

    # -- results -----------------------------------------------------------

    def _result(
        self,
        request: ModelModelOverlayWriteRequest,
        path: Path,
        model: _Model,
        outcome: str,
    ) -> ModelModelOverlayWriteResult:
        return ModelModelOverlayWriteResult(
            mode=request.mode,
            outcome=outcome,
            path=str(path),
            backend_ids=tuple(self._config["rebound_backends"]),
            served_model_id=model.served_model_id,
        )

    @staticmethod
    def _refused(
        request: ModelModelOverlayWriteRequest, path: Path | None, reason: str
    ) -> ModelModelOverlayWriteResult:
        return ModelModelOverlayWriteResult(
            mode=request.mode,
            outcome="refused",
            path=str(path) if path else None,
            reason=reason,
        )


def _tier_for(
    ollama: ModelOllamaConfig, memory_gb: int, model: str | None
) -> ModelOllamaModelTier:
    """The row naming ``model``, else the first row whose floor the memory reaches."""
    if model:
        named = next((t for t in ollama.models if t.model == model), None)
        if named is None:
            raise ValueError(f"{model!r} is not a model the ollama block declares")
        return named
    return next(t for t in ollama.models if t.min_memory_gb <= memory_gb)


__all__ = ["HandlerModelOverlayWrite", "node_config"]
