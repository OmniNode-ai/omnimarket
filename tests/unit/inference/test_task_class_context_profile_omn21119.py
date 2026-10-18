# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A task class may name the context profile its delegated requests carry (OMN-21119).

Plan task TA.18: the optional ``context_profile`` field on task classes, which
no production class sets until the profile is promoted, so the field is
exercised here by a fixture task class built on the shipped contract.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    load_task_class_authority,
    resolve_task_class_context_profile,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_profile_resolver_effect.models.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.nodes.node_context_profile_resolver_effect.models.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)
_FIXTURE_CLASS = "fixture_context_payload"


def _raw_contract() -> dict[str, Any]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _with_fixture_class(profile: object) -> dict[str, Any]:
    raw = _raw_contract()
    base = copy.deepcopy(raw["task_classes"]["code_generation"])
    base["context_profile"] = profile
    base["gateway_exposure"] = "internal"
    raw["task_classes"][_FIXTURE_CLASS] = base
    return raw


def test_no_production_task_class_names_a_context_profile() -> None:
    authority = load_task_class_authority()
    named = {
        name: entry.context_profile
        for name, entry in authority.task_classes.items()
        if entry.context_profile is not None
    }
    assert named == {}
    assert resolve_task_class_context_profile("code_generation") is None
    assert resolve_task_class_context_profile("no_such_class") is None


def test_fixture_task_class_names_the_delegation_payload_profile() -> None:
    authority = ModelTaskClassAuthority.model_validate(
        _with_fixture_class("delegation_payload_local")
    )
    entry = authority.task_classes[_FIXTURE_CLASS]
    assert entry.context_profile == "delegation_payload_local"
    # The class it was copied from is untouched: the field stays opt-in.
    assert authority.task_classes["code_generation"].context_profile is None


def test_profile_named_by_a_task_class_resolves_to_shipped_data() -> None:
    authority = ModelTaskClassAuthority.model_validate(
        _with_fixture_class("delegation_payload_local")
    )
    resolver = HandlerContextProfileResolver()
    for name, entry in authority.task_classes.items():
        if entry.context_profile is None:
            continue
        result = resolver.handle(
            ModelContextProfileResolveRequest(profile_name=entry.context_profile)
        )
        assert result.status is EnumContextProfileResolveStatus.OK, (
            name,
            result.errors,
        )


@pytest.mark.parametrize("bad", ["../etc/passwd", "Upper", "a/b", "", "x.yaml", 7])
def test_context_profile_that_is_not_a_profile_name_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="context_profile"):
        ModelTaskClassAuthority.model_validate(_with_fixture_class(bad))
