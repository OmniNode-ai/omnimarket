# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The triage default routing policy is bound to the model it names (OMN-24967).

The handler's pointer comment names ``ModelRoutingPolicy`` in ``omnibase_core`` and
its source path. The binding is checked against the imported model, not against
the comment's text alone: the default policy must validate as that model, and the
path the comment cites must be the file the imported model really comes from.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy
from pydantic import ValidationError

from omnimarket.nodes.node_merge_sweep_triage_orchestrator.handlers import (
    handler_triage,
)

_CITED_PATH = re.compile(
    r"\((src/omnibase_core/models/routing/model_routing_policy\.py)\)"
)


def test_default_routing_policy_validates_as_the_imported_model() -> None:
    policy = ModelRoutingPolicy.model_validate(handler_triage._DEFAULT_ROUTING_POLICY)
    assert policy.primary == handler_triage._DEFAULT_ROUTING_POLICY["primary"]
    assert policy.fallback == handler_triage._DEFAULT_ROUTING_POLICY["fallback"]
    assert policy.ci_override is not None


def test_a_binding_the_model_refuses_is_refused() -> None:
    """Control: the model forbids extras, so a drifted binding cannot validate."""
    drifted = {**handler_triage._DEFAULT_ROUTING_POLICY, "retired_field": 1}
    with pytest.raises(ValidationError):
        ModelRoutingPolicy.model_validate(drifted)


def test_the_cited_path_is_the_file_the_imported_model_comes_from() -> None:
    source = Path(handler_triage.__file__).read_text(encoding="utf-8")
    match = _CITED_PATH.search(source)
    assert match is not None, "the pointer comment no longer cites the model's path"
    cited = match.group(1).removeprefix("src/")
    actual = Path(inspect.getsourcefile(ModelRoutingPolicy) or "").as_posix()
    assert actual.endswith(cited)
