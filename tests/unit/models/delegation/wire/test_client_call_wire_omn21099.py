# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire fields and identity of a client call on the delegate-skill command.

The adapter plan's client-call identity (section 7.2): the key is
``sha256("claude-code:" + session_id + ":" + tool_use_id)``, the fingerprint is
the sha256 of the canonical request minus correlation and timestamps, and the
shared correlation id is ``uuid5(NS, key + ":" + fingerprint)``. The admission
event carries ids and verdict facts only, never prompt text.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_delegation_client_mode import EnumDelegationClientMode
from omnimarket.models.delegation.wire.model_delegate_skill_admission_decided import (
    ModelDelegateSkillAdmissionDecided,
)
from omnimarket.models.delegation.wire.model_delegate_skill_client_call import (
    CLIENT_CALL_CORRELATION_NAMESPACE,
    ModelDelegateSkillClientCall,
    client_call_correlation_id,
    client_call_key,
    request_fingerprint,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

SENTINEL = "SENTINEL-PROMPT-9f3a1c"


def _client_call(**overrides: object) -> ModelDelegateSkillClientCall:
    fields: dict[str, object] = {
        "client": "claude-code",
        "session_id": "sess-1",
        "tool_use_id": "toolu_1",
        "client_host": "lab-h202",
        "client_max_mode": "selective",
    }
    fields.update(overrides)
    return ModelDelegateSkillClientCall.model_validate(fields)


def _request(**overrides: object) -> ModelDelegateSkillRequest:
    fields: dict[str, object] = {
        "prompt": f"summarize {SENTINEL}",
        "task_type": "research",
        "source": "claude-code",
    }
    fields.update(overrides)
    return ModelDelegateSkillRequest.model_validate(fields)


def test_request_without_client_call_is_unchanged() -> None:
    request = _request()
    assert request.client_call is None
    assert "client_call" not in request.model_dump(mode="json", exclude_none=True)


def test_request_carries_client_call() -> None:
    request = _request(client_call=_client_call().model_dump(mode="json"))
    assert request.client_call is not None
    assert request.client_call.client_max_mode is EnumDelegationClientMode.SELECTIVE


@pytest.mark.parametrize("missing", ["session_id", "tool_use_id", "client_host"])
def test_client_call_refuses_absent_or_empty_ids(missing: str) -> None:
    # Absent ids mean no client_call at all, never a fabricated id (plan 7.2).
    with pytest.raises(ValidationError):
        _client_call(**{missing: ""})
    fields = _client_call().model_dump(mode="json")
    del fields[missing]
    with pytest.raises(ValidationError):
        ModelDelegateSkillClientCall.model_validate(fields)


def test_client_call_refuses_unknown_client_mode_and_extra_keys() -> None:
    with pytest.raises(ValidationError):
        _client_call(client_max_mode="always")
    with pytest.raises(ValidationError):
        _client_call(client="codex")
    with pytest.raises(ValidationError):
        _client_call(unexpected="x")


def test_client_modes_are_ordered_observe_selective_required() -> None:
    order = [
        EnumDelegationClientMode.OBSERVE,
        EnumDelegationClientMode.SELECTIVE,
        EnumDelegationClientMode.REQUIRED,
    ]
    assert sorted(order, key=lambda m: m.rank) == order
    assert [m.rank for m in order] == [0, 1, 2]
    assert EnumDelegationClientMode.OBSERVE.at_most(EnumDelegationClientMode.REQUIRED)
    assert not EnumDelegationClientMode.REQUIRED.at_most(
        EnumDelegationClientMode.SELECTIVE
    )


def test_client_call_key_is_the_documented_sha256() -> None:
    expected = hashlib.sha256(b"claude-code:sess-1:toolu_1").hexdigest()
    assert client_call_key(_client_call()) == expected


def test_client_call_key_ignores_host_and_mode() -> None:
    base = client_call_key(_client_call())
    assert client_call_key(_client_call(client_host="other")) == base
    assert client_call_key(_client_call(client_max_mode="required")) == base
    assert client_call_key(_client_call(tool_use_id="toolu_2")) != base


def test_fingerprint_ignores_correlation_and_timestamps_only() -> None:
    cc = _client_call().model_dump(mode="json")
    first = _request(
        client_call=cc,
        correlation_id=uuid.uuid4(),
        published_at=datetime(2026, 10, 18, tzinfo=UTC),
    )
    second = _request(
        client_call=cc,
        correlation_id=uuid.uuid4(),
        published_at=datetime(2026, 10, 19, tzinfo=UTC),
    )
    assert request_fingerprint(first) == request_fingerprint(second)
    changed = _request(client_call=cc, prompt="a different prompt")
    assert request_fingerprint(changed) != request_fingerprint(first)


def test_fingerprint_is_sha256_of_canonical_json_without_volatile_keys() -> None:
    request = _request(client_call=_client_call().model_dump(mode="json"))
    body = request.model_dump(mode="json", exclude_none=True)
    for volatile in ("correlation_id", "published_at", "trace_id", "span_id"):
        body.pop(volatile, None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert (
        request_fingerprint(request)
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )


def test_correlation_id_is_uuid5_of_key_and_fingerprint() -> None:
    request = _request(client_call=_client_call().model_dump(mode="json"))
    assert request.client_call is not None
    key = client_call_key(request.client_call)
    fingerprint = request_fingerprint(request)
    expected = uuid.uuid5(CLIENT_CALL_CORRELATION_NAMESPACE, f"{key}:{fingerprint}")
    assert client_call_correlation_id(request) == expected
    # Same key, different request: a different correlation id (plan 7.2).
    other = _request(client_call=_client_call().model_dump(mode="json"), prompt="other")
    assert client_call_correlation_id(other) != expected


def test_correlation_id_needs_a_client_call() -> None:
    with pytest.raises(ValueError, match="client_call"):
        client_call_correlation_id(_request())


def test_admission_decided_event_carries_no_prompt_text() -> None:
    request = _request(client_call=_client_call().model_dump(mode="json"))
    assert request.client_call is not None
    event = ModelDelegateSkillAdmissionDecided(
        correlation_id=client_call_correlation_id(request),
        client_call_key=client_call_key(request.client_call),
        request_fingerprint=request_fingerprint(request),
        admitted=True,
        task_type="research",
        reason="delegatable",
        effective_mode=EnumDelegationClientMode.SELECTIVE,
    )
    assert SENTINEL not in event.model_dump_json()
    assert set(event.model_dump(mode="json")) == {
        "correlation_id",
        "client_call_key",
        "request_fingerprint",
        "admitted",
        "task_type",
        "reason",
        "effective_mode",
    }
    with pytest.raises(ValidationError):
        ModelDelegateSkillAdmissionDecided.model_validate(
            {**event.model_dump(mode="json"), "prompt": "x"}
        )


@pytest.mark.parametrize("client_max", list(EnumDelegationClientMode))
@pytest.mark.parametrize("cohort", list(EnumDelegationClientMode))
def test_effective_mode_is_the_lesser_of_client_max_and_cohort(
    client_max: EnumDelegationClientMode, cohort: EnumDelegationClientMode
) -> None:
    effective = EnumDelegationClientMode.lesser(client_max, cohort)
    assert effective.at_most(client_max)
    assert effective.at_most(cohort)
    assert effective in (client_max, cohort)
