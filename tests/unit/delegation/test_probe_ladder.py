# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21212, OMN-23162: the local port takes a probe ladder only from synthetic probes.

An organic, unclassified or other-surface request carrying a probe ladder is
refused before dispatch: the stub transport sees no call and the run's one row
is a ``refused_before_dispatch`` failure. A synthetic request from the doctor's
escalation check or the readiness suite's probes walks the given rungs in
order, and the routing contract is never consulted.

OMN-23162 adds the ``faults`` mode: the request declares the typed ladder in
both modes, the handler forwards it, and a faults-mode ladder replaces no rung.
The production ladder is resolved as usual and a call to a model id the faults
name answers its transport fault instead of reaching the provider.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from pydantic import ValidationError

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.routing import delegation_backend_resolution as resolution_mod
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    PROBE_LADDER_WIRE_KEY,
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_probe_faults import (
    ModelProbeFault,
    ModelProbeFaults,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    PROBE_LADDER_SOURCE_SURFACE,
    PROBE_LADDER_SOURCE_SURFACES,
    READINESS_PROBE_SOURCE_SURFACE,
    ModelProbeLadder,
    ModelProbeRung,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.stub_probe_transport import (
    StubProbeReply,
    StubProbeTransport,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from tests.helpers.tenant_registry import mirror_tenant_in_store

pytestmark = pytest.mark.unit

_GOOD_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)
_FIRST = "http://127.0.0.1:41001/v1/chat/completions"
_SECOND = "http://127.0.0.1:41002/v1/chat/completions"
_LADDER = ModelProbeLadder(
    rungs=(
        ModelProbeRung(
            backend_id="probe-first", model_id="stub-a", endpoint_ref=_FIRST
        ),
        ModelProbeRung(
            backend_id="probe-second", model_id="stub-b", endpoint_ref=_SECOND
        ),
    )
)
_DOCTOR = ModelDelegationProvenance(
    source="claude-code",
    traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
    source_surface=PROBE_LADDER_SOURCE_SURFACE,
)


@pytest.fixture
def _routing_is_never_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rungs-mode probe run, accepted or refused, never asks the routing contract.

    A faults-mode run is the opposite: it resolves the production ladder, so the
    faults-mode tests below do not take this fixture.
    """

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a probe-ladder run consulted the routing contract")

    for name in (
        "first_eligible_tier",
        "next_eligible_tier",
        "backend_id_for_tier",
        "sibling_backend_available_in_tier",
        "resolve_delegation_backend",
    ):
        monkeypatch.setattr(port_mod, name, forbidden)


def _dispatch(
    tmp_path: Path,
    transport: StubProbeTransport,
    provenance: ModelDelegationProvenance | None,
) -> tuple[UUID, dict[str, Any]]:
    port = LocalDelegationDispatchPort(
        effect_handler=transport,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    correlation_id = uuid4()
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
            provenance=provenance,
            probe_ladder=_LADDER,
        )
    )
    return correlation_id, result


def _row(tmp_path: Path, correlation_id: UUID) -> sqlite3.Row:
    connection = sqlite3.connect(tmp_path / "delegation.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "the run wrote no delegation_events row"
    return row


def _transport(first: StubProbeReply) -> StubProbeTransport:
    return StubProbeTransport(
        replies={_FIRST: first, _SECOND: StubProbeReply.answer(_GOOD_ANSWER)}
    )


@pytest.mark.usefixtures("_routing_is_never_consulted")
@pytest.mark.parametrize(
    "provenance",
    [
        ModelDelegationProvenance(
            source="claude-code",
            traffic_class=EnumDelegationTrafficClass.ORGANIC,
            source_surface=PROBE_LADDER_SOURCE_SURFACE,
        ),
        ModelDelegationProvenance(
            source="claude-code",
            traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
            source_surface="some_other_probe",
        ),
        ModelDelegationProvenance(source="claude-code"),
        None,
    ],
    ids=["organic", "synthetic_other_surface", "unclassified", "no_provenance"],
)
def test_probe_ladder_refused_before_dispatch_with_zero_provider_calls(
    tmp_path: Path, provenance: ModelDelegationProvenance | None
) -> None:
    transport = _transport(StubProbeReply.answer(_GOOD_ANSWER))
    correlation_id = uuid4()
    port = LocalDelegationDispatchPort(
        effect_handler=transport,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )

    with pytest.raises(port_mod.ProtocolConfigurationError, match="probe ladder"):
        asyncio.run(
            port.dispatch(
                prompt="explain the tradeoff",
                task_type="research",
                correlation_id=correlation_id,
                max_tokens=256,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                execution_timeout_seconds=240,
                terminal_delivery_margin_seconds=60,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
                provenance=provenance,
                probe_ladder=_LADDER,
            )
        )

    assert transport.calls == []
    row = _row(tmp_path, correlation_id)
    assert not row["terminal_ok"]
    assert not row["quality_gate_passed"]
    assert row["terminal_failure_cause"] == "refused_before_dispatch"
    assert json.loads(row["attempt_history"] or "[]") == []


@pytest.mark.usefixtures("_routing_is_never_consulted")
def test_probe_ladder_synthetic_doctor_run_climbs_the_given_rungs(
    tmp_path: Path,
) -> None:
    transport = _transport(StubProbeReply.rate_limited())

    correlation_id, result = _dispatch(tmp_path, transport, _DOCTOR)

    assert result["status"] == "completed"
    assert transport.calls == [_FIRST, _SECOND]
    stored = json.loads(_row(tmp_path, correlation_id)["attempt_history"])
    assert [rung["backend_id"] for rung in stored] == ["probe-first", "probe-second"]
    assert [rung["model_id"] for rung in stored] == ["stub-a", "stub-b"]


@pytest.mark.usefixtures("_routing_is_never_consulted")
def test_probe_ladder_synthetic_doctor_run_stops_on_the_first_accepted_rung(
    tmp_path: Path,
) -> None:
    transport = _transport(StubProbeReply.answer(_GOOD_ANSWER))

    correlation_id, result = _dispatch(tmp_path, transport, _DOCTOR)

    assert result["status"] == "completed"
    assert transport.calls == [_FIRST]
    stored = json.loads(_row(tmp_path, correlation_id)["attempt_history"])
    assert [rung["backend_id"] for rung in stored] == ["probe-first"]


def test_probe_ladder_rungs_must_be_loopback_and_distinct() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        ModelProbeRung(
            backend_id="probe-remote",
            model_id="stub",
            endpoint_ref="https://api.example.com/v1/chat/completions",
        )
    with pytest.raises(ValidationError, match="repeats a backend id"):
        ModelProbeLadder(rungs=(_LADDER.rungs[0], _LADDER.rungs[0]))
    with pytest.raises(ValidationError):
        ModelProbeLadder(rungs=())


def _wire_request(**extra: object) -> dict[str, object]:
    return {
        "prompt": "summarise the module",
        "task_type": "research",
        "source": "claude-code",
        **extra,
    }


def test_probe_ladder_wire_key_is_declared_and_typed_in_both_modes() -> None:
    """OMN-23162: the request declares ``probe_ladder``, a rungs or a faults ladder.

    Step 2 of 2: the key OMN-23161 tolerated is now a typed field, so the
    ladder a producer sends reaches the handler as the model it named.
    """
    assert PROBE_LADDER_WIRE_KEY == "probe_ladder"
    assert PROBE_LADDER_WIRE_KEY in ModelDelegateSkillRequest.model_fields

    plain = ModelDelegateSkillRequest.model_validate(_wire_request())
    assert plain.probe_ladder is None
    assert PROBE_LADDER_WIRE_KEY not in plain.model_dump(mode="json")

    rungs = ModelDelegateSkillRequest.model_validate(
        _wire_request(**{PROBE_LADDER_WIRE_KEY: _LADDER.model_dump(mode="json")})
    )
    assert rungs.probe_ladder == _LADDER

    faults = ModelDelegateSkillRequest.model_validate(
        _wire_request(**{PROBE_LADDER_WIRE_KEY: _FAULTS.model_dump(mode="json")})
    )
    assert faults.probe_ladder == _FAULTS
    assert (
        ModelDelegateSkillRequest.model_validate_json(
            faults.model_dump_json()
        ).probe_ladder
        == _FAULTS
    )


def test_probe_ladder_null_is_tolerated_as_an_ordinary_request() -> None:
    plain = ModelDelegateSkillRequest.model_validate(_wire_request())
    carried = ModelDelegateSkillRequest.model_validate(
        _wire_request(**{PROBE_LADDER_WIRE_KEY: None})
    )
    assert carried.model_dump(
        mode="json", exclude={"correlation_id"}
    ) == plain.model_dump(mode="json", exclude={"correlation_id"})


def test_probe_ladder_wire_key_refuses_a_ladder_that_is_neither_mode() -> None:
    for bad in (
        {"rungs": []},
        {"faults": []},
        {"mode": "faults"},
        {"mode": "faults", "faults": [{"model_id": "a", "status_code": 200}]},
        {"rungs": [], "faults": []},
        {"not_a_ladder": 1},
    ):
        with pytest.raises(ValidationError):
            ModelDelegateSkillRequest.model_validate(
                _wire_request(**{PROBE_LADDER_WIRE_KEY: bad})
            )


def test_probe_ladder_declaration_does_not_widen_other_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="not_a_field"):
        ModelDelegateSkillRequest.model_validate(
            _wire_request(**{PROBE_LADDER_WIRE_KEY: None, "not_a_field": 1})
        )


def _gate_graded_keys() -> dict[str, list[str]]:
    """The key sets the Wire Compatibility Gate grades for the request model.

    Loads the gate's own probe from ``scripts/ci`` and describes the real wire
    module, so the keys read here are the keys the gate pushes through each
    pinned consumer.
    """
    probe_path = (
        Path(__file__).resolve().parents[3] / "scripts" / "ci" / "_wire_compat_probe.py"
    )
    spec = importlib.util.spec_from_file_location("_wire_compat_probe", probe_path)
    assert spec is not None
    assert spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    import omnimarket.models.delegation.wire.model_delegate_skill_request as wire

    return probe._describe(wire)["ModelDelegateSkillRequest"]


def test_probe_ladder_key_is_in_the_wire_gate_graded_keys_and_released_ladder_unchanged() -> (
    None
):
    """OMN-23162: the declared key is graded; the released ladder model is not widened.

    The gate pushes the maximal key set through each pinned consumer. The
    request's new key ``probe_ladder`` is accepted by a consumer pinned at or
    after OMN-23161. ``ModelProbeLadder`` keeps exactly its released keys
    (``rungs``), because a pinned consumer's copy forbids any other; the faults
    mode is a separate class in its own module.
    """
    graded = _gate_graded_keys()
    assert PROBE_LADDER_WIRE_KEY in graded["emitted_keys"]
    assert graded["extra"] == "forbid"
    assert sorted(ModelProbeLadder.model_fields) == ["rungs"]
    assert sorted(ModelProbeRung.model_fields) == [
        "backend_id",
        "endpoint_ref",
        "max_tokens",
        "model_id",
        "tier",
        "timeout_ms",
    ]


@pytest.mark.parametrize("surface", sorted(PROBE_LADDER_SOURCE_SURFACES))
@pytest.mark.usefixtures("_routing_is_never_consulted")
def test_probe_ladder_admitted_surfaces_climb_the_given_rungs(
    tmp_path: Path, surface: str
) -> None:
    provenance = ModelDelegationProvenance(
        source="claude-code",
        traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
        source_surface=surface,
    )
    transport = _transport(StubProbeReply.rate_limited())

    _, result = _dispatch(tmp_path, transport, provenance)

    assert result["status"] == "completed"
    assert transport.calls == [_FIRST, _SECOND]


def test_probe_ladder_admitted_surfaces_are_the_doctor_and_the_readiness_probe() -> (
    None
):
    assert {
        PROBE_LADDER_SOURCE_SURFACE,
        READINESS_PROBE_SOURCE_SURFACE,
    } == PROBE_LADDER_SOURCE_SURFACES
    assert READINESS_PROBE_SOURCE_SURFACE == "readiness_probe"


# --- faults mode (OMN-23162) --------------------------------------------------

_FAULTS = ModelProbeFaults(
    faults=(ModelProbeFault(model_id="model-b", status_code=429),)
)
_READINESS = ModelDelegationProvenance(
    source="claude-code",
    traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
    source_surface=READINESS_PROBE_SOURCE_SURFACE,
)
_PRODUCTION_ANSWER = (
    "### ANSWER\n"
    "The sky is blue because Rayleigh scattering by air molecules scatters "
    "short blue wavelengths of sunlight far more strongly than red ones."
)
_LOCAL_CODER = "http://127.0.0.1:18742/v1/chat/completions"
_LOCAL_HEAVY = "http://127.0.0.1:18743/v1/chat/completions"


def test_probe_faults_mode_models_are_typed() -> None:
    assert ModelProbeFaults.model_validate(_FAULTS.model_dump(mode="json")) == _FAULTS
    assert _FAULTS.fault_for("model-b") == _FAULTS.faults[0]
    assert _FAULTS.fault_for("model-a") is None
    assert _FAULTS.faults[0].failure_class is EnumDelegationFailureClass.RATE_LIMITED
    assert (
        ModelProbeFault(model_id="m", status_code=503).failure_class
        is EnumDelegationFailureClass.MODEL_UNAVAILABLE
    )
    with pytest.raises(ValidationError, match="repeat a model id"):
        ModelProbeFaults(faults=(_FAULTS.faults[0], _FAULTS.faults[0]))
    with pytest.raises(ValidationError, match="status must be one of"):
        ModelProbeFault(model_id="m", status_code=200)
    with pytest.raises(ValidationError):
        ModelProbeFaults(faults=())
    with pytest.raises(ValidationError):
        ModelProbeFaults.model_validate({"mode": "rungs", "faults": []})


@pytest.mark.parametrize(
    "provenance",
    [
        ModelDelegationProvenance(
            source="claude-code",
            traffic_class=EnumDelegationTrafficClass.ORGANIC,
            source_surface=READINESS_PROBE_SOURCE_SURFACE,
        ),
        ModelDelegationProvenance(
            source="claude-code",
            traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
            source_surface="some_other_probe",
        ),
        ModelDelegationProvenance(source="claude-code"),
        None,
    ],
    ids=["organic", "synthetic_other_surface", "unclassified", "no_provenance"],
)
def test_probe_faults_mode_refused_before_dispatch_with_zero_provider_calls(
    tmp_path: Path, provenance: ModelDelegationProvenance | None
) -> None:
    transport = StubProbeTransport(replies={})
    correlation_id = uuid4()
    port = LocalDelegationDispatchPort(
        effect_handler=transport,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )

    with pytest.raises(port_mod.ProtocolConfigurationError, match="probe ladder"):
        asyncio.run(
            port.dispatch(
                prompt="explain the tradeoff",
                task_type="research",
                correlation_id=correlation_id,
                max_tokens=256,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                execution_timeout_seconds=240,
                terminal_delivery_margin_seconds=60,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
                provenance=provenance,
                probe_ladder=_FAULTS,
            )
        )

    assert transport.calls == []
    row = _row(tmp_path, correlation_id)
    assert not row["terminal_ok"]
    assert row["terminal_failure_cause"] == "refused_before_dispatch"
    assert json.loads(row["attempt_history"] or "[]") == []


@pytest.fixture
def production_ladder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """The shipped routing contracts, two declared local models and every key present.

    The delegation unit suite's conftest hides the host overlay so resolution is
    hermetic; this fixture points it at the test's own overlay instead, so the
    production ladder resolves from the real contracts: ``document`` tries the
    declared heavy-reasoning model first, then the ladder above it.
    """
    overlay = tmp_path / "declared_models.yaml"
    overlay.write_text(
        "backends:\n"
        "  - backend_id: local-coder\n"
        f'    endpoint_url: "{_LOCAL_CODER}"\n'
        '    model_name: "model-a"\n'
        "  - backend_id: local-heavy-reasoning\n"
        f'    endpoint_url: "{_LOCAL_HEAVY}"\n'
        '    model_name: "model-b"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    monkeypatch.setattr(resolution_mod, "_OVERLAY_PATH", overlay, raising=False)
    for name in ("resolve_delegation_backend", "load_bifrost_backends"):
        kwdefaults = getattr(getattr(resolution_mod, name), "__kwdefaults__", None)
        if kwdefaults and "overlay_path" in kwdefaults:
            monkeypatch.setitem(kwdefaults, "overlay_path", overlay)
    monkeypatch.setattr(routing, "api_key_ref_available", lambda *_a, **_k: True)
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._load_bifrost_endpoints.cache_clear()


class _ProductionTransport:
    """A provider stand-in: answers every call, except a model id it is told 429s."""

    def __init__(self, *, rate_limited_model: str | None = None) -> None:
        self.rate_limited_model = rate_limited_model
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_id)
        if request.model_id == self.rate_limited_model:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.RATE_LIMITED,
                http_status=429,
                error_message="provider answered 429",
                latency_ms=1,
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_PRODUCTION_ANSWER,
            tokens_in=12,
            tokens_out=30,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _production_run(
    tmp_path: Path,
    transport: _ProductionTransport,
    probe_ladder: ModelProbeFaults | None,
) -> dict[str, Any]:
    mirror_tenant_in_store(tmp_path / "evidence.sqlite", HOUSE_TENANT_SLUG)
    port = LocalDelegationDispatchPort(
        effect_handler=transport,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="say ok",
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=HOUSE_TENANT_SLUG,
            provenance=_READINESS if probe_ladder is not None else None,
            probe_ladder=probe_ladder,
        )
    )
    return result


def _walked(result: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    return [
        (attempt["backend_id"], attempt["model_id"], attempt["failure_class"])
        for attempt in result["attempts"]
    ]


@pytest.mark.usefixtures("production_ladder")
def test_probe_faults_mode_readiness_probe_walks_the_resolved_production_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: model A answers 429, and production's own next rung answers.

    The control is production itself: the same run, unfaulted, on a provider that
    really answers 429 for model A. The faults-mode run walks the identical
    ladder, A rate-limited and then production's next rung accepted, and the
    routing contract is consulted throughout, which a rungs-mode run never does.
    """
    consulted: list[str] = []
    real_first_eligible_tier = port_mod.first_eligible_tier

    def spy(*args: Any, **kwargs: Any) -> Any:
        consulted.append("first_eligible_tier")
        return real_first_eligible_tier(*args, **kwargs)

    monkeypatch.setattr(port_mod, "first_eligible_tier", spy)

    unfaulted = _ProductionTransport()
    first = _production_run(tmp_path, unfaulted, None)
    assert first["status"] == "completed"
    assert unfaulted.calls == ["model-b"]

    really_429 = _ProductionTransport(rate_limited_model="model-b")
    expected = _production_run(tmp_path, really_429, None)
    assert expected["status"] == "completed"
    assert len(_walked(expected)) == 2

    consulted.clear()
    forced = _ProductionTransport()
    result = _production_run(tmp_path, forced, _FAULTS)

    assert result["status"] == "completed"
    assert consulted, "a faults-mode run must resolve the production ladder"
    assert _walked(result) == _walked(expected)
    walked = _walked(result)
    assert walked[0] == ("local-heavy-reasoning", "model-b", "rate_limited")
    assert walked[1][2] is None
    # The faulted model's call never reached the provider; the next rung's did.
    assert "model-b" not in forced.calls
    assert forced.calls == really_429.calls[1:]


@pytest.mark.usefixtures("production_ladder")
def test_probe_faults_mode_a_model_it_does_not_name_reaches_its_provider(
    tmp_path: Path,
) -> None:
    """Control: faults naming a model outside the run's ladder change nothing."""
    other = ModelProbeFaults(faults=(ModelProbeFault(model_id="model-z"),))
    transport = _ProductionTransport()

    result = _production_run(tmp_path, transport, other)

    assert result["status"] == "completed"
    assert transport.calls == ["model-b"]
    assert [a["failure_class"] for a in result["attempts"]] == [None]


def test_probe_faults_mode_is_forwarded_by_the_handler_and_only_when_carried() -> None:
    """The handler hands the request's ladder to the port, and only when it has one."""

    async def forwarded(request: ModelDelegateSkillRequest) -> dict[str, Any]:
        port = AsyncMock()
        port.dispatch.return_value = {"status": "completed", "content": "ok"}
        await HandlerDelegateSkill(object(), dispatch_port=port).handle(request)
        port.dispatch.assert_awaited_once()
        kwargs: dict[str, Any] = port.dispatch.await_args.kwargs
        return kwargs

    base = {"prompt": "say ok", "task_type": "research", "source": "claude-code"}
    for ladder in (_FAULTS, _LADDER):
        kwargs = asyncio.run(
            forwarded(ModelDelegateSkillRequest(**base, probe_ladder=ladder))
        )
        assert kwargs["probe_ladder"] == ladder
    kwargs = asyncio.run(forwarded(ModelDelegateSkillRequest(**base)))
    assert "probe_ladder" not in kwargs
