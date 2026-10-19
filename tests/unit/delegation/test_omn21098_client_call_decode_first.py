# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegate-skill request decodes ``client_call`` before it declares it.

Step 1 of 2 for the client-call identity the delegation adapter stamps on an
Agent call (context and delegation adapter plan, task TB.3). The wire
compatibility gate grades the maximal key set against the last release, and that
release forbids the key. So this release only teaches the request to read the
key: a null is the ordinary request and is dropped, and a set value is refused
by name, so a caller is never run as an ordinary delegation when it asked for a
client call. Step 2 declares the field once a release carrying this is out.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    CLIENT_CALL_WIRE_KEY,
    ModelDelegateSkillRequest,
)

pytestmark = pytest.mark.unit


def _request(**extra: object) -> dict[str, object]:
    return {
        "prompt": "summarise the module",
        "task_type": "research",
        "source": "claude-code",
        **extra,
    }


def test_the_wire_key_is_client_call() -> None:
    assert CLIENT_CALL_WIRE_KEY == "client_call"


def test_a_null_client_call_decodes_and_is_dropped() -> None:
    request = ModelDelegateSkillRequest.model_validate(
        _request(**{CLIENT_CALL_WIRE_KEY: None})
    )
    assert CLIENT_CALL_WIRE_KEY not in request.model_dump(mode="json")


def test_a_set_client_call_is_refused_by_name() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelDelegateSkillRequest.model_validate(
            _request(
                **{
                    CLIENT_CALL_WIRE_KEY: {
                        "client": "claude-code",
                        "session_id": "s-1",
                        "tool_use_id": "toolu_1",
                    }
                }
            )
        )
    assert CLIENT_CALL_WIRE_KEY in str(caught.value)
    assert "not honoured by this release" in str(caught.value)


def test_a_request_without_the_key_is_unchanged() -> None:
    plain = ModelDelegateSkillRequest.model_validate(_request())
    with_null = ModelDelegateSkillRequest.model_validate(
        _request(**{CLIENT_CALL_WIRE_KEY: None})
    )
    assert plain.model_dump(mode="json", exclude={"correlation_id"}) == (
        with_null.model_dump(mode="json", exclude={"correlation_id"})
    )


def test_an_unrelated_extra_key_is_still_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelDelegateSkillRequest.model_validate(_request(not_a_field=1))
    assert "extra_forbidden" in {e["type"] for e in caught.value.errors()}
