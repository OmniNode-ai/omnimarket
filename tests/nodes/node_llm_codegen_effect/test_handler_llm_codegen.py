# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_llm_codegen_effect — deterministic injected inference.

The EFFECT's network I/O is exercised through an injected ``ModelInferenceAdapter``
stub, so these run without a live model. They assert the generated source is
recorded on the pipeline state, that a fenced code block is unwrapped, and that
the prompt carries the spec's structural fields.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast

import pytest

from omnimarket.codegen.models import (
    ModelCodegenPipelineState,
    ModelCodegenSpec,
    ModelLlmGenerateCommand,
)
from omnimarket.inference.bridge_config_loader import resolve_bifrost_backend
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_llm_codegen_effect.handlers import handler_llm_codegen
from omnimarket.nodes.node_llm_codegen_effect.handlers.handler_llm_codegen import (
    HandlerLlmCodegen,
    _build_prompt,
    _strip_code_fences,
    build_lab_inference_adapter,
)


class _StubInference:
    """Deterministic inference double — duck-typed ``infer``, no ABC subclassing."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []
        self.system_prompts: list[str] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
        response_format: object | None = None,
        protocol_selection: object | None = None,
    ) -> str:
        self.calls.append((model_key, user_prompt))
        self.system_prompts.append(system_prompt)
        return self._response


def _command() -> ModelLlmGenerateCommand:
    spec = ModelCodegenSpec(
        node_name="NodeGreeterCompute",
        namespace="omninode.services.greeter.compute",
        archetype="compute",
        base_class="NodeCompute",
        handler_method="handle",
    )
    return ModelLlmGenerateCommand(state=ModelCodegenPipelineState(spec=spec))


@pytest.mark.asyncio
class TestGenerate:
    async def test_bare_response_is_recorded_on_state(self) -> None:
        stub = _StubInference("class NodeGreeterCompute:\n    pass\n")
        result = await HandlerLlmCodegen(stub).handle(_command())
        assert result.state.source_text == "class NodeGreeterCompute:\n    pass\n"
        # the spec is threaded through unchanged.
        assert result.state.spec.node_name == "NodeGreeterCompute"

    async def test_fenced_response_is_unwrapped(self) -> None:
        response = "sure:\n```python\nx = 1\n```\ntrailing prose\n"
        result = await HandlerLlmCodegen(_StubInference(response)).handle(_command())
        assert result.state.source_text == "x = 1\n"

    async def test_prompt_carries_structural_fields(self) -> None:
        stub = _StubInference("x = 1\n")
        await HandlerLlmCodegen(stub).handle(_command())
        _model_key, user_prompt = stub.calls[0]
        assert "NodeGreeterCompute" in user_prompt
        assert "NodeCompute" in user_prompt
        assert "handle" in user_prompt

    async def test_system_prompt_states_what_the_type_check_gate_requires(
        self,
    ) -> None:
        """The factory type-checks the module alone: an import outside the standard
        library or an unannotated function is rejected, so the model is told."""
        stub = _StubInference("x = 1\n")
        await HandlerLlmCodegen(stub).handle(_command())
        assert "standard library" in stub.system_prompts[0]
        assert "annotate" in stub.system_prompts[0]
        assert "declares no methods" in stub.system_prompts[0]


@pytest.mark.unit
class TestHelpers:
    def test_strip_code_fences_prefers_fenced_block(self) -> None:
        assert _strip_code_fences("a\n```python\ncode\n```\nb") == "code\n"

    def test_strip_code_fences_without_fence_returns_stripped(self) -> None:
        assert _strip_code_fences("  code here  ") == "code here\n"

    def test_build_prompt_includes_optional_hint(self) -> None:
        spec = ModelCodegenSpec(
            node_name="NodeX",
            namespace="ns",
            archetype="effect",
            prompt_hint="use httpx",
        )
        assert "use httpx" in _build_prompt(spec)


class _LabModelServer:
    """A local stand-in for the lab model server: records each request it gets."""

    def __init__(self, reply: str) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        recorded = self.requests

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                recorded.append((self.path, json.loads(self.rfile.read(length))))
                body = json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": reply}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _LabModelServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


@pytest.fixture
def lab_model() -> Iterator[_LabModelServer]:
    with _LabModelServer("```python\nx = 1\n```\n") as server:
        yield server


def _declare_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoint_url: str | None,
    model_name: str | None,
) -> None:
    backend = ModelDelegationBackendConfig(
        backend_id="local-coder",
        provider="local",
        tier="local",
        endpoint_url=endpoint_url,
        model_name=model_name,
    )
    monkeypatch.setattr(
        handler_llm_codegen,
        "resolve_bifrost_backend",
        lambda backend_id: backend if backend_id == "local-coder" else None,
    )


@pytest.mark.unit
class TestDefaultAdapterReachesTheLabModel:
    """Built with no injected adapter, the handler calls the routing contract's
    lab-hosted backend. It used to build the bridge from an empty config, which
    refuses the node's own model key (``Unknown model_key``)."""

    @pytest.mark.asyncio
    async def test_default_handler_generates_through_the_declared_endpoint(
        self, monkeypatch: pytest.MonkeyPatch, lab_model: _LabModelServer
    ) -> None:
        _declare_backend(
            monkeypatch,
            endpoint_url=f"{lab_model.url}/v1/chat/completions",
            model_name="Qwen3.8-27B",
        )

        result = await HandlerLlmCodegen().handle(_command())

        assert result.state.source_text == "x = 1\n"
        assert len(lab_model.requests) == 1
        path, body = lab_model.requests[0]
        assert path == "/v1/chat/completions"
        assert body["model"] == "Qwen3.8-27B"

    @pytest.mark.asyncio
    async def test_default_handler_turns_the_reasoning_trace_off(
        self, monkeypatch: pytest.MonkeyPatch, lab_model: _LabModelServer
    ) -> None:
        """Qwen3.8-27B on the lab spends the request's 2048-token budget thinking
        and returns an empty ``content``; the bridge then hands the reasoning text
        back as the source, which the validator rejects as a syntax error. The
        node asks for the no-think protocol profile so the module comes back in
        ``content``."""
        _declare_backend(
            monkeypatch,
            endpoint_url=f"{lab_model.url}/v1/chat/completions",
            model_name="Qwen3.8-27B",
        )

        await HandlerLlmCodegen().handle(_command())

        _path, body = lab_model.requests[0]
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        messages = cast("list[dict[str, str]]", body["messages"])
        assert messages[-1]["content"].startswith("/no_think")

    @pytest.mark.parametrize(
        ("endpoint_url", "model_name"),
        [
            (None, "Lab-Coder-1"),
            ("http://127.0.0.1:1/v1/chat/completions", None),
            (None, None),
        ],
    )
    @pytest.mark.asyncio
    async def test_backend_without_endpoint_or_model_fails_without_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        endpoint_url: str | None,
        model_name: str | None,
    ) -> None:
        _declare_backend(monkeypatch, endpoint_url=endpoint_url, model_name=model_name)

        with pytest.raises(RuntimeError, match="no lab model to call"):
            await HandlerLlmCodegen().handle(_command())

    def test_undeclared_backend_fails_without_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler_llm_codegen, "resolve_bifrost_backend", lambda _backend_id: None
        )

        with pytest.raises(RuntimeError, match="no lab model to call"):
            build_lab_inference_adapter()

    def test_construction_reads_no_routing_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(backend_id: str) -> None:
            raise AssertionError("the routing contract was read at construction")

        monkeypatch.setattr(handler_llm_codegen, "resolve_bifrost_backend", _fail)

        HandlerLlmCodegen()

    def test_shipped_routing_contract_keeps_the_backend_local(self) -> None:
        """The endpoint and served model come from the lane overlay, so the
        packaged contract carries none; what it must keep is that the backend this
        node generates with is a local one and not a paid provider's."""
        backend = resolve_bifrost_backend("local-coder")

        assert backend is not None
        assert backend.provider == "local"
        assert backend.tier == "local"
