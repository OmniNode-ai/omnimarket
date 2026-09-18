# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared rig every local-path chain pair runs on (OMN-18698).

What this is, and what it deliberately is not
---------------------------------------------
Every pair in this package drives the REAL dispatch path:
``HandlerDelegateSkill`` -> ``LocalDelegationDispatchPort`` ->
``HandlerLlmDelegationCall`` -> a real HTTP request -> the canonical SQLite
projection. Nothing between the request model and the socket is replaced by a
test double. That is the whole point of a chain pair: a mock of the handler
proves the mock (memory ``feedback_real_dispatch_path_tests``).

Three things ARE redirected, and each one is a deployment fact rather than a
behaviour:

``the provider host``
    :class:`LocalProviderStub` listens on ``127.0.0.1`` and speaks the OpenAI
    completion shape. It stands in for ``openrouter.ai`` because a test may not
    reach the internet, spend a customer's money, or depend on a third party's
    availability. It is a real socket answering real HTTP, so the transport,
    the ``/v1/models`` fail-closed guard, the status-line classification and
    the credential header all run exactly as they do in production.

``the BYOK catalogue's endpoint host``
    :func:`local_byok_catalogue` copies the SHIPPED
    ``byok_provider_backends.v1.yaml`` and rewrites ONE field on ONE row:
    ``openrouter``'s ``endpoint_url``, pointed at the stub above. Everything
    else -- the schema version, the model name, ``max_retries``, the budgets,
    the loader, the validator, the fail-closed behaviour for an undeclared
    provider -- is the real file read by the real code.
    :func:`assert_catalogue_differs_only_in_endpoint` pins that, so a pair
    cannot quietly drift into asserting against a catalogue of its own
    invention.

``the local store's path``
    :func:`use_local_store` points ``default_evidence_db_path`` at a tmp file.
    A customer machine has exactly one such file; a test host must not write
    to the operator's. The store code, the DDL and the ``ProtocolSecretStore``
    reads are untouched.

The house rung is supplied through the same seam the existing delegate golden
chain uses (``delegation_backend_resolution.load_bifrost_backends``) so a pair
states the routing precondition it is about -- "the ladder resolved a
house-keyed OpenRouter rung" -- instead of depending on which rung the shipped
ladder happens to prefer for a task type today.

Related:
    - OMN-18698: golden + error chain pairs for the local rows
    - OMN-18694 (L4), OMN-18695 (L5), OMN-18696 (L6)
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    BifrostBackendRef,
)
from omnimarket.routing import byok_provider_backends, delegation_backend_resolution
from tests.fixtures.judge_inference import CannedAdequacyBridge

#: The provider slug both the shipped catalogue and the house rung declare.
PROVIDER_SLUG = "openrouter"

#: The HOUSE credential reference the shipped OpenRouter rung carries. A
#: customer machine cannot resolve it, which is the fact L4's error chain is
#: about.
HOUSE_SECRET_REF = "llm.openrouter.api_key"

#: The backend id the shipped BYOK catalogue declares for ``openrouter``.
BYOK_BACKEND_ID = "byok-openrouter"

#: The house rung's backend id in ``bifrost_delegation.yaml``.
HOUSE_BACKEND_ID = "openrouter-qwen3-coder-480b"

#: Task type used by every pair. Declared on the rung's capabilities below.
TASK_TYPE = "code_generation"


# --------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------


class LocalProviderStub:
    """A real HTTP provider on the loopback, standing in for ``openrouter.ai``.

    Serves ``GET .../models`` (the effect handler's fail-closed served-model
    guard reads it) and ``POST .../chat/completions``. Records the
    ``Authorization`` header of every completion request so a pair can prove
    WHICH credential was presented without the test ever printing a value:
    the expected value is one the test itself registered a moment earlier.
    """

    def __init__(self, *, model_id: str, content: str = "print('ok')\n") -> None:
        self.model_id = model_id
        self.content = content
        #: ``401`` rejects every completion; ``200`` answers it.
        self.completion_status = 200
        #: Authorization header per completion request, in order. ``None`` for
        #: a request that carried none.
        self.authorizations: list[str | None] = []
        #: The decoded JSON body of every completion request, in order. What
        #: the model was actually shown, rather than what a caller intended.
        self.payloads: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:
                if not self.path.endswith("/models"):
                    self.send_response(404)
                    self.end_headers()
                    return
                self._respond(200, {"data": [{"id": outer.model_id}]})

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.authorizations.append(self.headers.get("Authorization"))
                try:
                    outer.payloads.append(json.loads(raw or b"{}"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    outer.payloads.append({})
                if outer.completion_status != 200:
                    self._respond(
                        outer.completion_status,
                        {
                            "error": {
                                "message": "Incorrect API key provided.",
                                "type": "invalid_request_error",
                            }
                        },
                    )
                    return
                self._respond(
                    200,
                    {
                        "choices": [
                            {
                                "message": {"content": outer.content},
                                "finish_reason": "stop",
                            }
                        ],
                        "model": outer.model_id,
                        "usage": {
                            "prompt_tokens": 21,
                            "completion_tokens": 37,
                            "total_tokens": 58,
                        },
                    },
                )

            def _respond(self, status: int, body: dict[str, object]) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- addressing --------------------------------------------------------

    @property
    def completions_url(self) -> str:
        assert self._server is not None, "stub not started"
        return f"http://127.0.0.1:{self._server.server_port}/v1/chat/completions"

    def last_message(self, role: str) -> str:
        """The content of the last request's message for ``role``, or ``""``.

        Reads what reached the provider, so a claim about what the model was
        shown is a fact about the wire and not about the caller's intent.
        """
        if not self.payloads:
            return ""
        for message in reversed(self.payloads[-1].get("messages") or []):
            if message.get("role") == role:
                return str(message.get("content") or "")
        return ""

    @property
    def presented_credential(self) -> str | None:
        """The bearer token of the last completion request, or ``None``."""
        if not self.authorizations:
            return None
        header = self.authorizations[-1]
        if header is None:
            return None
        prefix = "Bearer "
        return header[len(prefix) :] if header.startswith(prefix) else header


@pytest.fixture
def provider_stub() -> Iterator[LocalProviderStub]:
    """A started loopback provider serving the shipped BYOK model id."""
    stub = LocalProviderStub(model_id=shipped_byok_model_name())
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------


def _read_shipped_catalogue() -> dict[str, Any]:
    with open(byok_provider_backends.CATALOG_PATH) as handle:
        document: dict[str, Any] = yaml.safe_load(handle)
    return document


def shipped_byok_model_name() -> str:
    """The model id the shipped catalogue declares for ``openrouter``."""
    for row in _read_shipped_catalogue()["providers"]:
        if row["provider"] == PROVIDER_SLUG:
            return str(row["model_name"])
    raise AssertionError(
        f"the shipped BYOK catalogue no longer declares {PROVIDER_SLUG!r}; "
        "every pair in this package is about that row"
    )


def local_byok_catalogue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, endpoint_url: str
) -> Path:
    """Point the BYOK catalogue's OpenRouter row at ``endpoint_url``.

    The returned file is the SHIPPED document with exactly one scalar changed.
    Asserted, not asserted-to: see
    :func:`assert_catalogue_differs_only_in_endpoint`.
    """
    document = deepcopy(_read_shipped_catalogue())
    rewritten = 0
    for row in document["providers"]:
        if row["provider"] == PROVIDER_SLUG:
            row["endpoint_url"] = endpoint_url
            rewritten += 1
    assert rewritten == 1, (
        f"expected exactly one {PROVIDER_SLUG!r} row in the shipped catalogue, "
        f"found {rewritten}"
    )
    path = tmp_path / "byok_provider_backends.v1.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))

    assert_catalogue_differs_only_in_endpoint(path, endpoint_url)

    monkeypatch.setattr(byok_provider_backends, "CATALOG_PATH", path)
    # Drop the process-lifetime cache so the repoint takes. The matching clear
    # on the way out is the package's autouse ``_restore_byok_catalogue_cache``.
    byok_provider_backends.load_byok_provider_catalog.cache_clear()
    byok_provider_backends.load_byok_not_offered_providers.cache_clear()
    return path


def assert_catalogue_differs_only_in_endpoint(path: Path, endpoint_url: str) -> None:
    """Fail if the rig's catalogue differs from the shipped one anywhere else.

    Without this a pair could pass against a catalogue that had drifted away
    from the file a customer actually gets -- proving the rig rather than the
    product.
    """
    shipped = _read_shipped_catalogue()
    used: dict[str, Any] = yaml.safe_load(path.read_text())
    normalised = deepcopy(used)
    for row in normalised["providers"]:
        if row["provider"] == PROVIDER_SLUG:
            assert row["endpoint_url"] == endpoint_url
            row["endpoint_url"] = next(
                shipped_row["endpoint_url"]
                for shipped_row in shipped["providers"]
                if shipped_row["provider"] == PROVIDER_SLUG
            )
    assert normalised == shipped, (
        "the chain rig's BYOK catalogue differs from the shipped one in more "
        "than the OpenRouter endpoint host; a pair must exercise the shipped "
        "declaration, not one of its own"
    )


# --------------------------------------------------------------------------
# The house rung
# --------------------------------------------------------------------------


def install_rungs(monkeypatch: pytest.MonkeyPatch, rungs: list[dict[str, Any]]) -> None:
    """Make ``rungs`` the ONLY backends every routing surface can see.

    There are two of them and a pair that patches one is not deterministic.
    ``resolve_delegation_backend`` reads
    ``delegation_backend_resolution.load_bifrost_backends``; the tier ladder --
    ``first_eligible_tier``, ``next_eligible_tier``, ``backend_id_for_tier`` --
    reads ``handler_delegation_routing._load_bifrost_endpoints``, which loads
    the shipped contract MERGED WITH a host-local overlay at
    ``~/.omninode/delegation/bifrost_overrides.yaml``.

    That overlay is why an earlier revision of these pairs was green here and
    red on CI: with it, ``cheap_cloud`` had a resolvable endpoint and the
    ladder could climb; without it the ladder had nowhere to go, the L6
    positive control went red, and the two "did not climb" assertions beside it
    were left resting on a ladder that could not have climbed anyway. Which
    rungs exist is one fact, so both surfaces are given the same one and
    neither reads the host.
    """
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: deepcopy(rungs),
    )
    refs = {
        str(rung["backend_id"]): BifrostBackendRef(
            endpoint_url=str(rung["endpoint_url"]),
            model_name=str(rung["model_name"]),
            timeout_ms=int(rung["timeout_ms"]),
            max_tokens=int(rung["max_tokens"]),
            provider=rung.get("provider"),
            api_key_ref=rung.get("secret_ref"),
        )
        for rung in rungs
    }
    monkeypatch.setattr(
        handler_delegation_routing, "_load_bifrost_endpoints", lambda: dict(refs)
    )


def house_openrouter_rung(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Make the tier ladder resolve the house-keyed OpenRouter rung.

    Mirrors ``bifrost_delegation.yaml``'s ``openrouter-qwen3-coder-480b``: the
    same backend id, the same house ``secret_ref``, the same tier and budgets.
    Supplied through the loader seam so a pair asserts the SUBSTITUTION rather
    than which rung today's ladder prefers for a task type.
    """
    rung: dict[str, Any] = {
        "backend_id": HOUSE_BACKEND_ID,
        "provider": PROVIDER_SLUG,
        "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
        "model_name": shipped_byok_model_name(),
        "secret_ref": HOUSE_SECRET_REF,
        "tier": "cheap_frontier",
        "timeout_ms": 300000,
        "max_tokens": 65536,
        "capabilities": [TASK_TYPE, "test"],
    }
    install_rungs(monkeypatch, [rung])
    return rung


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


def use_local_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the local install's single SQLite file at ``tmp_path``.

    Both halves read it through the same name: the routing half resolves a
    REFERENCE from it, and the effect boundary resolves that reference's VALUE
    from it. One patch covers both because on a customer machine it is one
    file.
    """
    from omnimarket.inference import local_byok_credential_adapter

    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        local_byok_credential_adapter, "default_evidence_db_path", lambda: db_path
    )
    return db_path


# --------------------------------------------------------------------------
# The chain
# --------------------------------------------------------------------------


@contextmanager
def no_ambient_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Remove every env var that could answer an OpenRouter credential.

    A pair that proves "the customer's key answered" must not be able to pass
    because the operator's shell happened to export one.
    """
    for name in (
        "OPENROUTER_API_KEY",
        "LLM_OPENROUTER_API_KEY",
        HOUSE_SECRET_REF,
    ):
        monkeypatch.delenv(name, raising=False)
    yield


async def run_local_delegation(
    *,
    prompt: str,
    db_path: Path,
    correlation_id: UUID,
    adequacy_score: float = 0.95,
    task_type: str = TASK_TYPE,
    backend_id: str | None = HOUSE_BACKEND_ID,
    response_contract: dict[str, object] | None = None,
) -> ModelDelegateSkillResponse:
    """Drive the real chain end to end and return the typed terminal.

    ``effect_process_boundary=False`` runs the effect handler in-process, the
    same setting the existing delegate golden chain uses; the handler, its
    transport and its classification are the production ones either way.

    The judge is injected with a canned adequacy score for one reason: the
    quality gate's verdict is L11's subject, not L4/L5/L6's. A pair about a
    credential must not go red because a stub provider's canned sentence did
    not persuade a live judge.

    ``backend_id`` defaults to the HOUSE OpenRouter rung, using the request
    model's own caller-supplied pin (OMN-15156). Every pair here is about what
    happens to a house-keyed rung on a customer machine, so which rung the
    ladder would otherwise prefer is a precondition to STATE, not one to
    inherit: ``first_eligible_tier`` consults a stored ROI overlay, so the
    untargeted answer depends on what previous runs on this host recorded. The
    pin affects the INITIAL attempt only -- escalation still excludes the
    pinned backend's tier and re-resolves through the declared order -- so the
    no-climb assertions keep their meaning.
    """
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(
            inference_bridge=CannedAdequacyBridge(adequacy_score=adequacy_score)
        ),
    )
    handler = HandlerDelegateSkill(dispatch_port=port)
    return await handler.handle(
        ModelDelegateSkillRequest(
            prompt=prompt,
            task_type=task_type,
            source="claude-code",
            correlation_id=correlation_id,
            backend_id=backend_id,
            response_contract=response_contract,
        )
    )


__all__ = [
    "BYOK_BACKEND_ID",
    "HOUSE_BACKEND_ID",
    "HOUSE_SECRET_REF",
    "PROVIDER_SLUG",
    "TASK_TYPE",
    "LocalProviderStub",
    "assert_catalogue_differs_only_in_endpoint",
    "house_openrouter_rung",
    "install_rungs",
    "local_byok_catalogue",
    "no_ambient_provider_credentials",
    "provider_stub",
    "run_local_delegation",
    "shipped_byok_model_name",
    "use_local_store",
]
