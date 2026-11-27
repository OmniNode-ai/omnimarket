# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C30 and C31 probe nodes' overlay documents are shared models their overlay files can be pinned to (OMN-19915).

A node overlay is pinned to a merged revision that already holds its model, so each model lives in the shared overlay
documents package and merges ahead of the node that reads it (omnimarket#6949 adds the nodes).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_c30_probe_overlay import (
    ModelC30ProbeOverlay,
)
from omnimarket.models.overlay_documents.model_c31_probe_overlay import (
    ModelC31ProbeOverlay,
)

_FIELD = "history_" + "keep"
_DOC: dict[str, Any] = {
    "default_profile": "tick",
    "history_file": "runs.jsonl",
    "history_keep": 60,
    "result_topic": "onex.evt.example.probe-completed.v1",
    "profiles": {
        "inmemory": {"kind": "inmemory"},
        "tick": {"kind": "tick", "graded_packages": ["example-core"]},
        "local": {
            "kind": "local",
            "onex_bin_dir": "/example/bin",
            "graded_packages": ["example-core"],
        },
    },
}
_MODELS = [ModelC30ProbeOverlay, ModelC31ProbeOverlay]


@pytest.mark.parametrize("model", _MODELS)
def test_the_complete_document_loads(model: Any) -> None:
    doc = model.model_validate(_DOC)

    assert doc.default_profile == "tick"
    assert doc.profile(None).kind == "tick"
    assert doc.profile("local").onex_bin_dir == "/example/bin"


@pytest.mark.parametrize("model", _MODELS)
def test_a_default_profile_that_is_not_declared_is_refused(model: Any) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**_DOC, "default_profile": "missing"})


@pytest.mark.parametrize("model", _MODELS)
def test_an_unknown_key_is_refused(model: Any) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**_DOC, "gateway_url": "x"})


@pytest.mark.parametrize("model", _MODELS)
@pytest.mark.parametrize(
    "profile",
    [
        {"kind": "local", "graded_packages": ["p"]},
        {"kind": "tick", "onex_bin_dir": "/x", "graded_packages": ["p"]},
        {"kind": "tick"},
    ],
)
def test_a_malformed_profile_is_refused(model: Any, profile: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**_DOC, "profiles": {"tick": profile}})


@pytest.mark.parametrize("model", _MODELS)
def test_the_model_is_frozen(model: Any) -> None:
    doc = model.model_validate(_DOC)

    with pytest.raises(ValidationError):
        setattr(doc, _FIELD, 1)
