# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe-ladder legs the delegation readiness scenario nodes share (OMN-23173).

A *leg* is one synthetic delegation run through ``HandlerDelegateSkill`` with a rungs-mode probe ladder: each rung is a
loopback stub with a scripted behaviour (answers, answers a status, is stopped, is killed mid-call, is slow). The leg
returns the typed facts a scenario asserts on: the run status, cause and reason, the run's duration, and for every
attempt the backend, the failure class and whether it was accepted.

Two backends, one seam (``ProtocolLegTransport``):

* the in-memory default, ``ScriptedLegTransport``: each rung answers its scripted typed reply, no socket is opened;
* the real backend, ``LoopbackStubServers``: one stub HTTP server per rung on a loopback port, called by the real
  delegation effect, so a stopped, killed or slow rung is classified by production's own code.

Nothing here reads a clock for a verdict, an environment variable or a file outside the work directory it is given.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol
from uuid import UUID, uuid4, uuid5

from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_probe_rung_credential import EnumProbeRungCredential
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.handlers.execution_budget_overlay import EXECUTION_BUDGET_OVERLAY_ID
from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    OVERLAY_ROOTS_ENV,
)
from omnimarket.models.delegation.model_execution_budget_overlay import (
    ModelExecutionBudgetOverlay,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    READINESS_PROBE_SOURCE_SURFACE,
)
from omnimarket.models.delegation.wire.model_probe_ladder_credentialed import (
    ModelProbeLadderCredentialed,
    ModelProbeRungCredentialed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

Behaviour = Literal["answer", "status", "stopped", "killed", "slow"]
CostClass = Literal["free", "paid"]


class ModelRungScript(BaseModel):
    """One rung of a leg: who it is and how the stub behind it behaves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    behaviour: Behaviour
    #: The status a ``status`` rung answers on every call (503, 429, ...).
    status_code: int | None = Field(default=None, ge=400, le=599)
    #: How long a ``slow`` rung waits before it would answer.
    delay_ms: int = Field(default=0, ge=0)
    #: The rung's own deadline as the ladder file declares it.
    timeout_ms: int = Field(default=30000, ge=1)
    cost_class: CostClass = "free"
    tier: str = Field(default="local", min_length=1)
    #: The rung's output-token cap; ``None`` leaves the probe rung's own default.
    max_tokens: int | None = Field(default=None, ge=1)
    #: What the in-memory backend books for an answered call on this rung (a paid rung's scripted, measured cost).
    scripted_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    #: The state the rung declares for its provider credential; absent rungs are never attempted (OMN-23511).
    credential: EnumProbeRungCredential = EnumProbeRungCredential.NOT_REQUIRED


class ModelLegSpec(BaseModel):
    """One leg: a name, the prompt, the rungs in order, and the answer the healthy rung gives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    task_type: str = Field(default="research", min_length=1)
    prompt: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    rungs: tuple[ModelRungScript, ...] = Field(min_length=1)
    #: The request's own timeout in seconds, as ``onex delegate --timeout`` sets it; ``None`` leaves the class ceiling.
    requested_timeout_seconds: int | None = Field(default=None, ge=1)
    #: ``True`` runs the leg under an overlay root of its own: the overlay below when it is set, no overlay when it is not.
    #: The host's own overlay roots are then not read, so the leg says the same on every host.
    isolate_overlay_root: bool = False
    budget_overlay: ModelExecutionBudgetOverlay | None = None


class ModelAttemptFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str
    failure_class: str | None
    accepted: bool
    #: Facts of the attempt record a scenario may assert on; all default so an existing reader is unaffected.
    tier: str | None = None
    cost_usd: float = 0.0
    usage_source: str | None = None
    #: The accept/climb/retry stamp the dispatch port appends (``transition=<kind>:<cause>[:<move_kind>]``).
    acceptance_detail: str = ""
    http_status: int | None = None
    size_band: str | None = None


class ModelLegOutcome(BaseModel):
    """What one leg observed, all of it typed fields of the terminal; nothing is judged here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg: str
    status: str
    terminal_failure_cause: str | None
    terminal_failure_reason: str | None
    execution_duration_ms: int
    cost_savings_usd: float | None
    final_answer: str
    attempts: tuple[ModelAttemptFact, ...]
    #: The run's total spend as the terminal's metrics report it.
    cost_usd: float = 0.0
    #: Budget evidence of the terminal: where the ceiling came from, the ceiling the run was held to, what was asked and, on a
    #: refusal before dispatch, the refusal's reason.
    budget_source: str | None = None
    budget_ceiling_seconds: int | None = None
    budget_requested_seconds: int | None = None
    budget_refusal_reason: str | None = None

    @property
    def walked(self) -> tuple[str, ...]:
        return tuple(attempt.backend_id for attempt in self.attempts)


class ProtocolLegTransport(Protocol):
    """Gives each rung of a leg an endpoint and, for the in-memory backend, the effect that answers it."""

    def endpoints(self, leg: ModelLegSpec) -> dict[str, str]:
        """backend id -> endpoint url on a loopback host."""
        ...

    def effect(
        self,
    ) -> Callable[[ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult] | None:
        """The effect handler to inject, or ``None`` for the real delegation effect."""
        ...

    def virtual_elapsed_ms(self) -> int:
        """Milliseconds a rung is scripted to have taken without a clock running (0 for a real backend)."""
        ...

    def close(self) -> None: ...


_STATUS_CLASS: dict[int, EnumDelegationFailureClass] = {
    429: EnumDelegationFailureClass.RATE_LIMITED,
    500: EnumDelegationFailureClass.MODEL_UNAVAILABLE,
    502: EnumDelegationFailureClass.MODEL_UNAVAILABLE,
    503: EnumDelegationFailureClass.MODEL_UNAVAILABLE,
    504: EnumDelegationFailureClass.MODEL_UNAVAILABLE,
}
_FAILURE_FOR_BEHAVIOUR: dict[str, EnumDelegationFailureClass] = {
    "stopped": EnumDelegationFailureClass.MODEL_UNAVAILABLE,
    "killed": EnumDelegationFailureClass.MODEL_UNAVAILABLE,
    "slow": EnumDelegationFailureClass.TIMEOUT,
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ScriptedLegTransport:
    """The in-memory default: every rung answers its scripted typed reply; the effect is a pure function."""

    def __init__(self, host: str = "localhost") -> None:
        self._host = host
        self._scripts: dict[str, ModelRungScript] = {}
        self._answers: dict[str, str] = {}
        self._virtual_elapsed_ms = 0
        self.calls: list[str] = []

    def endpoints(self, leg: ModelLegSpec) -> dict[str, str]:
        urls: dict[str, str] = {}
        for index, rung in enumerate(leg.rungs):
            url = f"http://{self._host}:{41000 + index}/{leg.name}/{rung.backend_id}"
            urls[rung.backend_id] = url
            self._scripts[url] = rung
            self._answers[url] = leg.answer
        return urls

    def effect(
        self,
    ) -> Callable[[ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult]:
        def call(
            request: ModelLlmDelegationCallRequest,
        ) -> ModelLlmDelegationCallResult:
            self.calls.append(request.endpoint_ref)
            rung = self._scripts[request.endpoint_ref]
            answers_late = rung.behaviour == "slow" and rung.delay_ms < rung.timeout_ms
            if rung.behaviour == "answer" or answers_late:
                self._virtual_elapsed_ms += rung.delay_ms if answers_late else 0
                return _result(
                    request,
                    content=self._answers[request.endpoint_ref],
                    cost_usd=rung.scripted_cost_usd,
                )
            failure = (
                _STATUS_CLASS[rung.status_code or 503]
                if rung.behaviour == "status"
                else _FAILURE_FOR_BEHAVIOUR[rung.behaviour]
            )
            return _result(request, failure=failure, status=rung.status_code)

        return call

    def virtual_elapsed_ms(self) -> int:
        return self._virtual_elapsed_ms

    def close(self) -> None:
        return None


def _result(
    request: ModelLlmDelegationCallRequest,
    *,
    content: str = "",
    failure: EnumDelegationFailureClass | None = None,
    status: int | None = None,
    cost_usd: Decimal = Decimal("0"),
) -> ModelLlmDelegationCallResult:
    failed = failure is not None
    return ModelLlmDelegationCallResult(
        request_id=request.request_id,
        success=not failed,
        content=content,
        tokens_in=0 if failed else 11,
        tokens_out=0 if failed else 22,
        latency_ms=1,
        actual_cost_usd=cost_usd,
        savings_usd=Decimal("0"),
        usage_source=EnumUsageSource.MEASURED
        if cost_usd > 0
        else EnumUsageSource.UNKNOWN,
        http_status=status,
        failure_class=failure,
        error_message=f"scripted {failure.value}" if failure is not None else None,
    )


class LoopbackStubServers:
    """The real backend: one stub HTTP server per rung, on a loopback port, called by the real delegation effect."""

    def __init__(self, host: str = "localhost") -> None:
        self._host = host
        self._servers: list[ThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []

    def endpoints(self, leg: ModelLegSpec) -> dict[str, str]:
        urls: dict[str, str] = {}
        for rung in leg.rungs:
            if rung.behaviour == "stopped":
                port = _free_port()
            else:
                server = ThreadingHTTPServer(
                    (self._host, 0), _stub_handler(rung, leg.answer)
                )
                server.daemon_threads = True
                port = int(server.server_address[1])
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self._servers.append(server)
                self._threads.append(thread)
            urls[rung.backend_id] = f"http://{self._host}:{port}/v1/chat/completions"
        return urls

    def effect(self) -> None:
        return None

    def virtual_elapsed_ms(self) -> int:
        return 0

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._servers.clear()
        self._threads.clear()


def _stub_handler(rung: ModelRungScript, answer: str) -> type[BaseHTTPRequestHandler]:
    class Stub(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def _send_json(self, status: int, body: dict[str, object]) -> None:
            raw = json.dumps(body).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                # The caller gave up on a slow rung before it answered; that is the leg under test.
                return

        def do_GET(self) -> None:
            # The delegation effect probes <base>/health before it calls a rung.
            self._send_json(200, {"status": "ok"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if rung.behaviour == "killed":
                # Promise a body, send half of it, and drop the connection: a process killed mid-call.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "4096")
                self.end_headers()
                self.wfile.write(b'{"choices": [{"mess')
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if rung.behaviour == "slow":
                time.sleep(rung.delay_ms / 1000)
            if rung.behaviour == "status":
                self._send_json(rung.status_code or 503, {"error": "scripted"})
                return
            self._send_json(
                200,
                {
                    "id": "readiness-stub",
                    "model": rung.model_id,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": answer},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 22,
                        "total_tokens": 33,
                    },
                },
            )

    return Stub


def seed_probe_tenant(db_path: Path, slug: str) -> None:
    """Mirror the probe tenant into the evidence store the run writes to, as the registry projection does."""
    from omnimarket.handlers.local_identity_mirror import write_mirror_row
    from omnimarket.local_deployment.tenant_identity import _ensure_tables_on
    from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

    adapter = SqliteDatabaseAdapter(db_path)
    _ensure_tables_on(adapter)
    write_mirror_row(
        adapter,
        tenant_uuid=uuid5(UUID(int=0), slug),
        tenant_slug=slug,
        now=datetime.now(tz=UTC),
    )


def run_leg(
    leg: ModelLegSpec,
    transport: ProtocolLegTransport,
    *,
    work_dir: Path,
    tenant_slug: str,
    source_surface: str = READINESS_PROBE_SOURCE_SURFACE,
    execution_timeout_seconds: int | None = None,
) -> ModelLegOutcome:
    """Run one leg through the skill handler and return the typed facts of its terminal."""
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / f"{leg.name}.sqlite"
    seed_probe_tenant(db_path, tenant_slug)
    urls = transport.endpoints(leg)
    ladder = ModelProbeLadderCredentialed(
        rungs=tuple(
            ModelProbeRungCredentialed(
                backend_id=rung.backend_id,
                model_id=rung.model_id,
                endpoint_ref=urls[rung.backend_id],
                tier=rung.tier,
                timeout_ms=rung.timeout_ms,
                credential=rung.credential,
                **({"max_tokens": rung.max_tokens} if rung.max_tokens else {}),
            )
            for rung in leg.rungs
        )
    )
    port = LocalDelegationDispatchPort(
        effect_handler=transport.effect(),
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    request = ModelDelegateSkillRequest(
        prompt=leg.prompt,
        task_type=leg.task_type,
        source="claude-code",
        correlation_id=uuid4(),
        tenant_id=tenant_slug,
        provenance=ModelDelegationProvenance(
            source="claude-code",
            traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
            source_surface=source_surface,
        ),
        probe_ladder=ladder,
        requested_timeout_seconds=leg.requested_timeout_seconds,
    )
    handler = HandlerDelegateSkill(dispatch_port=port)
    with _overlay_scope(leg, work_dir):
        response = asyncio.run(handler.handle(request))
    if response is None:
        raise RuntimeError(f"leg {leg.name} produced no terminal")
    evidence = response.budget_evidence
    refusal = response.budget_refusal
    return ModelLegOutcome(
        leg=leg.name,
        status=response.status,
        terminal_failure_cause=(
            response.terminal_failure_cause.value
            if response.terminal_failure_cause is not None
            else None
        ),
        terminal_failure_reason=response.terminal_failure_reason,
        execution_duration_ms=(response.execution_duration_ms or 0)
        + transport.virtual_elapsed_ms(),
        cost_savings_usd=response.metrics.cost_savings_usd,
        final_answer=response.response,
        attempts=tuple(
            ModelAttemptFact(
                backend_id=attempt.backend_id,
                failure_class=attempt.failure_class,
                accepted=attempt.quality_gate_passed,
                tier=attempt.tier,
                cost_usd=attempt.cost_usd,
                usage_source=(
                    attempt.usage_source.value
                    if attempt.usage_source is not None
                    else None
                ),
                acceptance_detail=attempt.acceptance_detail,
                http_status=attempt.http_status,
                size_band=(
                    str(getattr(attempt.size_band, "value", attempt.size_band))
                    if attempt.size_band is not None
                    else None
                ),
            )
            for attempt in response.attempts
        ),
        cost_usd=response.metrics.cost_usd,
        budget_source=response.budget_source,
        budget_ceiling_seconds=(
            evidence.task_class_timeout_ceiling_seconds
            if evidence is not None
            else refusal.task_class_timeout_ceiling_seconds
            if refusal is not None
            else None
        ),
        budget_requested_seconds=(
            evidence.requested_timeout_seconds
            if evidence is not None
            else refusal.requested_timeout_seconds
            if refusal is not None
            else None
        ),
        budget_refusal_reason=(refusal.reason.value if refusal is not None else None),
    )


@contextmanager
def _overlay_scope(leg: ModelLegSpec, work_dir: Path) -> Iterator[None]:
    """Point the overlay roots at a directory of the leg's own for the run, and put the host's value back after."""
    if not leg.isolate_overlay_root:
        yield
        return
    root = work_dir / f"{leg.name}-overlay-root"
    overlay_file = root / EXECUTION_BUDGET_OVERLAY_ID / OVERLAY_FILE_NAME
    if leg.budget_overlay is not None:
        overlay_file.parent.mkdir(parents=True, exist_ok=True)
        overlay_file.write_text(
            leg.budget_overlay.model_dump_json(exclude_none=True), encoding="utf-8"
        )
    else:
        root.mkdir(parents=True, exist_ok=True)
    before = os.environ.get(OVERLAY_ROOTS_ENV)
    os.environ[OVERLAY_ROOTS_ENV] = str(root)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(OVERLAY_ROOTS_ENV, None)
        else:
            os.environ[OVERLAY_ROOTS_ENV] = before


class LegTransportScope:
    """Context manager so a transport is always closed, whatever the leg did."""

    def __init__(self, transport: ProtocolLegTransport) -> None:
        self.transport = transport

    def __enter__(self) -> ProtocolLegTransport:
        return self.transport

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.transport.close()
