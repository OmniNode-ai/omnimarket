# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegate-skill wire models declare the output-file keys (OMN-19599).

OMN-19600 was step 1: it taught both models to decode ``declared_outputs``,
``output_manifest`` and ``output_files`` while the wire compatibility gate
(OMN-18868) still graded against a release that forbade them. That release is
out (v0.4.225), so this change is step 2 and declares the fields. These tests
pin the declared shape: the keys are real fields, optional, and absent from the
wire when unset, so a request or terminal that does not use them is
byte-identical to what it was.
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
from omnimarket.models.delegation.wire.model_delegation_output_files import (
    ModelDelegationOutputManifest,
    compute_manifest_sha256,
)

pytestmark = pytest.mark.unit

_DECLARED = "declared_outputs"
_MANIFEST = "output_manifest"
_FILES = "output_files"


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


def test_the_new_keys_are_declared_fields() -> None:
    assert _DECLARED in ModelDelegateSkillRequest.model_fields
    assert _MANIFEST in ModelDelegateSkillResponse.model_fields
    assert _FILES in ModelDelegateSkillResponse.model_fields


def test_a_null_declared_outputs_key_decodes_and_is_not_emitted() -> None:
    request = ModelDelegateSkillRequest.model_validate(_request(**{_DECLARED: None}))
    assert request.declared_outputs is None
    assert _DECLARED not in request.model_dump(mode="json")


def test_a_real_declaration_decodes_and_is_emitted() -> None:
    request = ModelDelegateSkillRequest.model_validate(
        _request(**{_DECLARED: {"files": [{"path": "a.py", "kind": "code"}]}})
    )
    assert request.declared_outputs is not None
    assert [f.path for f in request.declared_outputs.files] == ["a.py"]
    assert request.model_dump(mode="json")[_DECLARED]["files"][0]["path"] == "a.py"


def test_an_unsafe_declared_path_is_refused_at_the_request() -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest.model_validate(
            _request(**{_DECLARED: {"files": [{"path": "../a.py", "kind": "code"}]}})
        )


def test_a_terminal_without_files_omits_both_keys() -> None:
    dumped = ModelDelegateSkillResponse.model_validate(_response()).model_dump(
        mode="json"
    )
    assert _MANIFEST not in dumped
    assert _FILES not in dumped


def test_the_manifest_and_files_decode_and_are_emitted() -> None:
    manifest = ModelDelegationOutputManifest(
        entries=(), refusals=(), manifest_sha256=compute_manifest_sha256(())
    )
    response = ModelDelegateSkillResponse.model_validate(
        _response(**{_MANIFEST: manifest.model_dump(mode="json"), _FILES: []})
    )
    assert response.output_manifest == manifest
    assert response.output_files == ()
    dumped = response.model_dump(mode="json")
    assert dumped[_MANIFEST]["manifest_sha256"] == manifest.manifest_sha256
    assert dumped[_FILES] == []


def test_an_unrelated_extra_key_is_still_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelDelegateSkillResponse.model_validate(_response(not_a_field=1))
    assert "extra_forbidden" in {e["type"] for e in caught.value.errors()}
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest.model_validate(_request(not_a_field=1))
