# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Identity derivations of a client call on the delegate-skill command.

The adapter plan's client-call identity (section 7.2): the fingerprint is the
sha256 of the canonical request minus correlation and timestamps, and the shared
correlation id is ``uuid5(NS, key + ":" + fingerprint)``. The effective mode is
the lesser of the client maximum and the cohort mode. The wire fields and the
key itself are covered by ``test_client_call_wire_step1.py``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest

from omnimarket.models.claude_code_binding import EnumClientMaxMode
from omnimarket.models.delegation.wire.model_delegate_skill_admission_decided import (
    ModelDelegateSkillAdmissionDecided,
)
from omnimarket.models.delegation.wire.model_delegate_skill_client_call import (
    CLIENT_CALL_CORRELATION_NAMESPACE,
    ModelDelegateSkillClientCall,
    client_call_correlation_id,
    client_mode_rank,
    lesser_client_mode,
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


def test_client_modes_are_ordered_observe_selective_required() -> None:
    order = [
        EnumClientMaxMode.OBSERVE,
        EnumClientMaxMode.SELECTIVE,
        EnumClientMaxMode.REQUIRED,
    ]
    assert sorted(order, key=client_mode_rank) == order
    assert [client_mode_rank(m) for m in order] == [0, 1, 2]


def test_fingerprint_ignores_correlation_and_timestamps_only() -> None:
    first = _request(
        correlation_id=uuid.uuid4(),
        published_at=datetime(2026, 10, 18, tzinfo=UTC),
    )
    second = _request(
        correlation_id=uuid.uuid4(),
        published_at=datetime(2026, 10, 19, tzinfo=UTC),
    )
    assert request_fingerprint(first) == request_fingerprint(second)
    changed = _request(prompt="a different prompt")
    assert request_fingerprint(changed) != request_fingerprint(first)


def test_fingerprint_is_sha256_of_canonical_json_without_volatile_keys() -> None:
    request = _request()
    body = request.model_dump(mode="json", exclude_none=True)
    for volatile in ("correlation_id", "published_at", "trace_id", "span_id"):
        body.pop(volatile, None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert (
        request_fingerprint(request)
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )


def test_correlation_id_is_uuid5_of_key_and_fingerprint() -> None:
    call = _client_call()
    request = _request()
    expected = uuid.uuid5(
        CLIENT_CALL_CORRELATION_NAMESPACE,
        f"{call.client_call_key}:{request_fingerprint(request)}",
    )
    assert client_call_correlation_id(call, request) == expected
    # Same key, different request: a different correlation id (plan 7.2).
    assert client_call_correlation_id(call, _request(prompt="other")) != expected
    # Same request, different tool call: a different correlation id.
    other_call = _client_call(tool_use_id="toolu_2")
    assert client_call_correlation_id(other_call, request) != expected


def test_correlation_namespace_is_the_documented_value() -> None:
    assert (
        uuid.uuid5(uuid.NAMESPACE_URL, "onex:omnimarket:delegate-skill:client-call")
        == CLIENT_CALL_CORRELATION_NAMESPACE
    )


def test_admission_decided_event_carries_no_prompt_text() -> None:
    call = _client_call()
    request = _request()
    event = ModelDelegateSkillAdmissionDecided(
        correlation_id=client_call_correlation_id(call, request),
        client_call_key=call.client_call_key,
        request_fingerprint=request_fingerprint(request),
        admitted=True,
        task_type="research",
        reason="delegatable",
        effective_mode=lesser_client_mode(
            call.client_max_mode, EnumClientMaxMode.REQUIRED
        ),
    )
    assert SENTINEL not in event.model_dump_json()
    assert event.effective_mode is EnumClientMaxMode.SELECTIVE


@pytest.mark.parametrize("client_max", list(EnumClientMaxMode))
@pytest.mark.parametrize("cohort", list(EnumClientMaxMode))
def test_effective_mode_is_the_lesser_of_client_max_and_cohort(
    client_max: EnumClientMaxMode, cohort: EnumClientMaxMode
) -> None:
    effective = lesser_client_mode(client_max, cohort)
    assert client_mode_rank(effective) <= client_mode_rank(client_max)
    assert client_mode_rank(effective) <= client_mode_rank(cohort)
    assert effective in (client_max, cohort)
