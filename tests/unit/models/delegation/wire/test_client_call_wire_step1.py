# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TB.3 wire slice: the client-call identity, the admission event and step 1.

The delegate-skill request and terminal cannot declare the client-call keys in
the same release that first emits them: the Wire Compatibility Gate grades the
maximal key set against the deployed consumer. Step 1 (this slice) teaches both
models to decode the keys; step 2 declares them once a release carrying step 1
is the pinned consumer.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.claude_code_binding import EnumClientMaxMode
from omnimarket.models.delegation.wire.model_delegate_skill_admission_decided import (
    ModelDelegateSkillAdmissionDecided,
)
from omnimarket.models.delegation.wire.model_delegate_skill_client_call import (
    ModelDelegateSkillClientCall,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)

_CONTRACT = (
    Path(__file__).resolve().parents[5]
    / "src/omnimarket/nodes/node_delegate_skill_orchestrator/contract.yaml"
)
_ADMISSION_TOPIC = "onex.evt.omnimarket.delegate-skill-admission-decided.v1"
_ECHO_KEYS = ("admitted", "effective_mode", "client_call_key", "request_fingerprint")
_HEX64 = "a" * 64


def _client_call(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "client": "claude-code",
        "session_id": "sess-1",
        "tool_use_id": "toolu_01",
        "client_host": "lab-h202",
        "client_max_mode": "observe",
    }
    payload.update(overrides)
    return payload


def _request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prompt": "summarise this",
        "task_type": "research",
        "source": "claude-code",
    }
    payload.update(overrides)
    return payload


def _terminal(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "completed",
        "correlation_id": str(uuid4()),
        "task_type": "research",
    }
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_client_call_key_is_sha256_of_client_session_and_tool_use() -> None:
    call = ModelDelegateSkillClientCall.model_validate(_client_call())

    expected = hashlib.sha256(b"claude-code:sess-1:toolu_01").hexdigest()

    assert call.client_call_key == expected


@pytest.mark.unit
def test_client_call_key_changes_with_each_identity_part() -> None:
    base = ModelDelegateSkillClientCall.model_validate(_client_call()).client_call_key
    other_session = ModelDelegateSkillClientCall.model_validate(
        _client_call(session_id="sess-2")
    ).client_call_key
    other_tool = ModelDelegateSkillClientCall.model_validate(
        _client_call(tool_use_id="toolu_02")
    ).client_call_key
    other_host = ModelDelegateSkillClientCall.model_validate(
        _client_call(client_host="mac")
    ).client_call_key

    assert len({base, other_session, other_tool}) == 3
    # The host is routing data, not identity: the same Agent call is the same call.
    assert other_host == base


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["session_id", "tool_use_id", "client_host"])
def test_client_call_never_fabricates_an_id(missing: str) -> None:
    payload = _client_call()
    del payload[missing]

    with pytest.raises(ValidationError):
        ModelDelegateSkillClientCall.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize("empty", ["session_id", "tool_use_id", "client_host"])
def test_client_call_refuses_empty_ids(empty: str) -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillClientCall.model_validate(_client_call(**{empty: ""}))


@pytest.mark.unit
def test_client_call_modes_are_the_binding_modes_and_extras_forbidden() -> None:
    for mode in EnumClientMaxMode:
        call = ModelDelegateSkillClientCall.model_validate(
            _client_call(client_max_mode=mode.value)
        )
        assert call.client_max_mode is mode
    with pytest.raises(ValidationError):
        ModelDelegateSkillClientCall.model_validate(
            _client_call(client_max_mode="always")
        )
    with pytest.raises(ValidationError):
        ModelDelegateSkillClientCall.model_validate(_client_call(prompt="leak"))


@pytest.mark.unit
def test_admission_event_carries_the_plan_fields_and_no_prompt_text() -> None:
    correlation_id = uuid4()
    event = ModelDelegateSkillAdmissionDecided.model_validate(
        {
            "correlation_id": str(correlation_id),
            "client_call_key": _HEX64,
            "request_fingerprint": "b" * 64,
            "admitted": True,
            "task_type": "research",
            "reason": "allowlisted",
            "effective_mode": "selective",
        }
    )

    assert set(ModelDelegateSkillAdmissionDecided.model_fields) == {
        "correlation_id",
        "client_call_key",
        "request_fingerprint",
        "admitted",
        "task_type",
        "reason",
        "effective_mode",
    }
    assert event.effective_mode is EnumClientMaxMode.SELECTIVE
    assert event.correlation_id == correlation_id
    with pytest.raises(ValidationError):
        ModelDelegateSkillAdmissionDecided.model_validate(
            {**event.model_dump(mode="json"), "prompt": "the Agent prompt"}
        )


@pytest.mark.unit
@pytest.mark.parametrize("field", ["client_call_key", "request_fingerprint"])
@pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "a" * 63])
def test_admission_event_hashes_are_lowercase_sha256_hex(field: str, bad: str) -> None:
    payload = {
        "correlation_id": str(uuid4()),
        "client_call_key": _HEX64,
        "request_fingerprint": _HEX64,
        "admitted": False,
        "task_type": None,
        "reason": "sensitive",
        "effective_mode": "observe",
    }
    payload[field] = bad

    with pytest.raises(ValidationError):
        ModelDelegateSkillAdmissionDecided.model_validate(payload)


@pytest.mark.unit
def test_request_without_client_call_is_unchanged() -> None:
    request = ModelDelegateSkillRequest.model_validate(_request())
    explicit_null = ModelDelegateSkillRequest.model_validate(_request(client_call=None))

    assert "client_call" not in request.model_dump(mode="json")
    assert "client_call" not in explicit_null.model_dump(mode="json")
    assert "client_call" not in ModelDelegateSkillRequest.model_fields
    assert explicit_null.model_dump(mode="json") == {
        **request.model_dump(mode="json"),
        "correlation_id": str(explicit_null.correlation_id),
    }


@pytest.mark.unit
def test_request_with_client_call_is_refused_by_name_never_dropped() -> None:
    with pytest.raises(ValidationError, match="client_call"):
        ModelDelegateSkillRequest.model_validate(
            _request(client_call=_client_call(client_max_mode="selective"))
        )


@pytest.mark.unit
def test_terminal_without_echo_keys_is_unchanged() -> None:
    terminal = ModelDelegateSkillResponse.model_validate(_terminal())

    dumped = terminal.model_dump(mode="json")

    assert not set(_ECHO_KEYS) & set(dumped)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("admitted", True),
        ("effective_mode", "selective"),
        ("client_call_key", _HEX64),
        ("request_fingerprint", _HEX64),
    ],
)
def test_terminal_decodes_each_echo_key_before_it_is_declared(
    key: str, value: object
) -> None:
    terminal = ModelDelegateSkillResponse.model_validate(_terminal(**{key: value}))

    assert key not in terminal.model_dump(mode="json")


@pytest.mark.unit
def test_terminal_decodes_the_whole_echo_set_at_once() -> None:
    terminal = ModelDelegateSkillResponse.model_validate(
        _terminal(
            admitted=True,
            effective_mode="required",
            client_call_key=_HEX64,
            request_fingerprint=_HEX64,
        )
    )

    assert not set(_ECHO_KEYS) & set(terminal.model_dump(mode="json"))


@pytest.mark.unit
def test_terminal_still_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillResponse.model_validate(_terminal(client_call_keys=_HEX64))


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_contract_publishes_the_admission_decided_topic() -> None:
    contract = _contract()

    assert _ADMISSION_TOPIC in contract["event_bus"]["publish_topics"]
    published = {
        entry["event_type"]: entry["topic"] for entry in contract["published_events"]
    }
    assert published["DelegateSkillAdmissionDecided"] == _ADMISSION_TOPIC
    # The runtime resolves a published event by its class name less ``Model``.
    assert (
        ModelDelegateSkillAdmissionDecided.__name__.removeprefix("Model")
        == "DelegateSkillAdmissionDecided"
    )


@pytest.mark.unit
def test_admission_topic_is_an_evt_topic_and_not_the_terminal() -> None:
    contract = _contract()

    assert _ADMISSION_TOPIC.startswith("onex.evt.")
    assert contract["terminal_event"] != _ADMISSION_TOPIC
    assert _ADMISSION_TOPIC not in contract["event_bus"]["subscribe_topics"]
    # Its consumer is the adapter's client, outside every omnimarket contract.
    assert _ADMISSION_TOPIC in contract["externally_consumed_topics"]
