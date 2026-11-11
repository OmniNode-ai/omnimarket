# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23511: a probe rung declares, as a typed enum, that its credential is absent.

The dispatch port never attempts such a rung -- no provider call, the same skip
production takes for a rung whose credential has no value (OMN-23170). The run
names the rung under ``skipped_rungs`` and moves on to the next one. A ladder
whose every rung is absent is refused before any provider call.

Golden chain: the absent rung is skipped and the next rung answers. Error chain:
a ladder with nothing attemptable is refused; the injected break leaves the
declaration off and shows the port then calls the rung, so the declaration is
what skips it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_probe_rung_credential import EnumProbeRungCredential
from omnimarket.models.delegation.model_escalation_transitions import (
    EnumEscalationCause,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    ModelProbeLadder,
    ModelProbeRung,
)
from omnimarket.models.delegation.wire.model_probe_ladder_credentialed import (
    ModelProbeLadderCredentialed,
    ModelProbeRungCredentialed,
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
from tests.unit.delegation.test_probe_ladder import (
    _DOCTOR,
    _GOOD_ANSWER,
    _row,
    _wire_request,
)

pytestmark = pytest.mark.unit

_ONE = "http://127.0.0.1:41011/v1/chat/completions"
_TWO = "http://127.0.0.1:41012/v1/chat/completions"
_THREE = "http://127.0.0.1:41013/v1/chat/completions"
_ABSENT = EnumProbeRungCredential.DECLARED_ABSENT
_PRESENT = EnumProbeRungCredential.NOT_REQUIRED


def _ladder(
    *rungs: tuple[str, str, EnumProbeRungCredential],
) -> ModelProbeLadderCredentialed:
    return ModelProbeLadderCredentialed(
        rungs=tuple(
            ModelProbeRungCredentialed(
                backend_id=backend_id,
                model_id=f"stub-{backend_id}",
                endpoint_ref=url,
                credential=credential,
            )
            for backend_id, url, credential in rungs
        )
    )


def _dispatch(
    tmp_path: Path, transport: StubProbeTransport, ladder: ModelProbeLadderCredentialed
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
            provenance=_DOCTOR,
            probe_ladder=ladder,
        )
    )
    return correlation_id, result


def test_the_credential_declaration_is_a_two_valued_enum_defaulting_to_not_required() -> (
    None
):
    assert {member.value for member in EnumProbeRungCredential} == {
        "not_required",
        "declared_absent",
    }
    rung = ModelProbeRungCredentialed(backend_id="a", model_id="m", endpoint_ref=_ONE)
    assert rung.credential is EnumProbeRungCredential.NOT_REQUIRED


def test_the_credential_declaration_takes_the_enum_wire_value_and_nothing_free() -> (
    None
):
    base = {"backend_id": "a", "model_id": "m", "endpoint_ref": _ONE}
    assert (
        ModelProbeRungCredentialed.model_validate(
            {**base, "credential": "declared_absent"}
        ).credential
        is EnumProbeRungCredential.DECLARED_ABSENT
    )
    for free_string in ("none", "absent", ""):
        with pytest.raises(ValidationError):
            ModelProbeRungCredentialed.model_validate(
                {**base, "credential": free_string}
            )


def test_the_released_probe_rung_shape_is_unchanged_and_the_credential_rides_a_new_version() -> (
    None
):
    """The wire gate's seam: a pinned consumer's ``ModelProbeRung`` forbids extra keys.

    The released rung and ladder keep exactly their released keys; the credential
    declaration lives only on the credentialed subclasses, so a payload that does
    not declare it decodes on the pinned consumer unchanged (OMN-23511).
    """
    assert "credential" not in ModelProbeRung.model_fields
    assert sorted(ModelProbeLadder.model_fields) == ["rungs"]
    assert sorted(ModelProbeLadderCredentialed.model_fields) == ["rungs"]
    assert set(ModelProbeRungCredentialed.model_fields) - set(
        ModelProbeRung.model_fields
    ) == {"credential"}
    base = {"backend_id": "a", "model_id": "m", "endpoint_ref": _ONE}
    with pytest.raises(ValidationError):
        ModelProbeRung.model_validate({**base, "credential": "declared_absent"})
    request_ladder = ModelDelegateSkillRequest.model_validate(
        _wire_request(
            probe_ladder={"rungs": [{**base, "credential": "declared_absent"}]}
        )
    ).probe_ladder
    assert isinstance(request_ladder, ModelProbeLadderCredentialed)
    assert request_ladder.rungs[0].credential is _ABSENT


@pytest.mark.usefixtures("_routing_is_never_consulted")
def test_golden_chain_an_absent_middle_rung_is_skipped_and_the_next_rung_answers(
    tmp_path: Path,
) -> None:
    transport = StubProbeTransport(
        replies={
            _ONE: StubProbeReply.rate_limited(),
            _TWO: StubProbeReply.answer("must never be asked"),
            _THREE: StubProbeReply.answer(_GOOD_ANSWER),
        }
    )
    ladder = _ladder(
        ("one", _ONE, _PRESENT), ("two", _TWO, _ABSENT), ("three", _THREE, _PRESENT)
    )

    correlation_id, result = _dispatch(tmp_path, transport, ladder)

    assert result["status"] == "completed"
    assert transport.calls == [_ONE, _THREE]
    skipped = result["skipped_rungs"]
    assert [rung["backend_id"] for rung in skipped] == ["two"]
    assert skipped[0]["rule"] == EnumEscalationCause.CREDENTIAL_MISSING.value
    assert "credential absent" in skipped[0]["reason"]
    stored = json.loads(_row(tmp_path, correlation_id)["attempt_history"])
    assert [rung["backend_id"] for rung in stored] == ["one", "three"]


@pytest.mark.usefixtures("_routing_is_never_consulted")
def test_golden_chain_an_absent_first_rung_is_skipped_and_the_second_rung_answers(
    tmp_path: Path,
) -> None:
    transport = StubProbeTransport(
        replies={
            _ONE: StubProbeReply.answer("never"),
            _TWO: StubProbeReply.answer(_GOOD_ANSWER),
        }
    )
    ladder = _ladder(("one", _ONE, _ABSENT), ("two", _TWO, _PRESENT))

    _, result = _dispatch(tmp_path, transport, ladder)

    assert result["status"] == "completed"
    assert transport.calls == [_TWO]
    assert [rung["backend_id"] for rung in result["skipped_rungs"]] == ["one"]


@pytest.mark.usefixtures("_routing_is_never_consulted")
def test_error_chain_a_ladder_with_every_rung_absent_is_refused_with_no_provider_call(
    tmp_path: Path,
) -> None:
    transport = StubProbeTransport(
        replies={
            _ONE: StubProbeReply.answer("never"),
            _TWO: StubProbeReply.answer("never"),
        }
    )
    ladder = _ladder(("one", _ONE, _ABSENT), ("two", _TWO, _ABSENT))
    port = LocalDelegationDispatchPort(
        effect_handler=transport,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    correlation_id = uuid4()

    with pytest.raises(port_mod.ProtocolConfigurationError, match="credential absent"):
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
                provenance=_DOCTOR,
                probe_ladder=ladder,
            )
        )

    assert transport.calls == []
    row = _row(tmp_path, correlation_id)
    assert not row["terminal_ok"]


@pytest.mark.usefixtures("_routing_is_never_consulted")
def test_error_chain_injected_break_without_the_declaration_the_port_calls_the_rung(
    tmp_path: Path,
) -> None:
    """The failing control: the same ladder with the declaration left off attempts the middle rung."""
    transport = StubProbeTransport(
        replies={
            _ONE: StubProbeReply.rate_limited(),
            _TWO: StubProbeReply.rate_limited(),
            _THREE: StubProbeReply.answer(_GOOD_ANSWER),
        }
    )
    ladder = _ladder(
        ("one", _ONE, _PRESENT), ("two", _TWO, _PRESENT), ("three", _THREE, _PRESENT)
    )

    _, result = _dispatch(tmp_path, transport, ladder)

    assert transport.calls == [_ONE, _TWO, _THREE]
    assert not result.get("skipped_rungs")
