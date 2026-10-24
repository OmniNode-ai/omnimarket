# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21212: the local port takes a probe ladder only from synthetic doctor traffic.

An organic, unclassified or other-surface request carrying a probe ladder is
refused before dispatch: the stub transport sees no call and the run's one row
is a ``refused_before_dispatch`` failure. A synthetic request from the doctor's
escalation check walks the given rungs in order, and the routing contract is
never consulted.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_probe_ladder import (
    PROBE_LADDER_SOURCE_SURFACE,
    ModelProbeLadder,
    ModelProbeRung,
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


@pytest.fixture(autouse=True)
def _routing_is_never_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe run, accepted or refused, never asks the routing contract."""

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
