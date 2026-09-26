# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegate-skill wire models decode the output-file keys before declaring them (OMN-19600).

OMN-19602 declares ``declared_outputs`` on ``ModelDelegateSkillRequest`` and
``output_manifest`` / ``output_files`` on ``ModelDelegateSkillResponse``. The
wire compatibility gate (OMN-18868) replays the maximal key set through the
LAST RELEASED model, which forbids extras, so declaring them in one step is
refused. This release is step 1, the consumer: it decodes the keys.

On the request, a non-null ``declared_outputs`` asks for a behaviour this
release does not perform (writing files). Dropping it would hand the caller a
text-only result it did not ask for, silently, so it is refused by name. On
the response, the manifest and files are information a consumer that predates
them cannot use, so they are dropped and the rest of the terminal decodes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)

pytestmark = pytest.mark.unit

# Literals, so this module collects against a tree without the tolerance and
# fails on behaviour, not on import.
_DECLARED = "declared_outputs"
_MANIFEST = "output_manifest"
_FILES = "output_files"
_WIRE_SHAPE_ERROR_TYPES = frozenset({"extra_forbidden", "missing"})


def _request(**extra: object) -> dict[str, object]:
    return {
        "prompt": "write a slug helper",
        "task_type": "code_generation",
        "source": "external-client",
        **extra,
    }


def _response(**extra: object) -> dict[str, object]:
    return {
        "status": "failed",
        "correlation_id": "00000000-0000-4000-8000-000000019600",
        "task_type": "code_generation",
        **extra,
    }


def test_a_null_declared_outputs_key_is_decoded_and_dropped() -> None:
    request = ModelDelegateSkillRequest.model_validate(_request(**{_DECLARED: None}))
    assert _DECLARED not in request.model_dump()


def test_a_real_declaration_is_refused_by_name_not_as_an_extra_key() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelDelegateSkillRequest.model_validate(
            _request(**{_DECLARED: {"files": [{"path": "a.py", "kind": "code"}]}})
        )
    assert _DECLARED in str(caught.value)
    assert not {e["type"] for e in caught.value.errors()} & _WIRE_SHAPE_ERROR_TYPES


def test_the_response_decodes_and_drops_the_manifest_and_files() -> None:
    response = ModelDelegateSkillResponse.model_validate(
        _response(
            **{
                _MANIFEST: {"entries": [], "refusals": [], "manifest_sha256": "x"},
                _FILES: [{"path": "a.py", "content": "x"}],
            }
        )
    )
    dumped = response.model_dump()
    assert _MANIFEST not in dumped
    assert _FILES not in dumped


def test_an_unrelated_extra_key_is_still_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelDelegateSkillResponse.model_validate(_response(not_a_field=1))
    assert "extra_forbidden" in {e["type"] for e in caught.value.errors()}
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest.model_validate(_request(not_a_field=1))


def test_neither_model_emits_a_new_key() -> None:
    assert _DECLARED not in ModelDelegateSkillRequest.model_fields
    assert _MANIFEST not in ModelDelegateSkillResponse.model_fields
    assert _FILES not in ModelDelegateSkillResponse.model_fields
