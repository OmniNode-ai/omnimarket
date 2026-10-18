# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The v5 context profiles under pack/profiles (OMN-21131, plan task TC.8, section 4.4)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.enums.enum_context_binding_kind import EnumContextBindingKind
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_profile_resolver_effect.models.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.nodes.node_context_profile_resolver_effect.models.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)
from omnimarket.pack.model_context_profile import ModelContextProfile

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

F = EnumContextFactor
SRC = Path(__file__).resolve().parents[2] / "src" / "omnimarket"
PROFILES_DIR = SRC / "pack" / "profiles"
PROFILE_NAMES = sorted(path.stem for path in PROFILES_DIR.glob("*.yaml"))
SECTION_4_4_PROFILES = frozenset(
    {"compaction_orchestrator", "session_start", "agent_spawn", "per_prompt"}
)


def _resolve(name: str) -> ModelContextProfile:
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name=name)
    )
    assert result.status is EnumContextProfileResolveStatus.OK, result.errors
    assert result.profile is not None
    return result.profile


def test_the_section_4_4_profiles_are_present() -> None:
    assert set(PROFILE_NAMES) >= SECTION_4_4_PROFILES


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_every_profile_validates_and_resolves_by_name(name: str) -> None:
    profile = _resolve(name)
    assert profile.content_hash() == _resolve(name).content_hash()


@pytest.mark.parametrize(
    ("name", "injection_point", "required", "optional", "budget", "latency"),
    [
        (
            "session_start",
            "SessionStart",
            {F.SESSION_GOAL, F.OPERATOR_RULINGS, F.OPEN_WORK},
            {F.CAPABILITY_DIGEST, F.INVARIANTS, F.PLAN_EXCERPTS},
            6000,
            5000,
        ),
        (
            "agent_spawn",
            "SubagentStart",
            {F.LANE_RULES},
            {F.INVARIANTS, F.CANONICAL_SHAPES, F.EXEMPLAR},
            4000,
            3000,
        ),
        (
            "per_prompt",
            "UserPromptSubmit",
            set(),
            {F.ARCHITECTURE_PATTERNS, F.PLAN_EXCERPTS, F.EXEMPLAR},
            1500,
            500,
        ),
    ],
)
def test_the_additive_profiles_carry_the_section_4_4_values(
    name: str,
    injection_point: str,
    required: set[EnumContextFactor],
    optional: set[EnumContextFactor],
    budget: int,
    latency: int,
) -> None:
    profile = _resolve(name)
    assert profile.injection_point == injection_point
    assert profile.binding_kind is EnumContextBindingKind.ADDITIVE
    assert set(profile.required_factors) == required
    assert set(profile.optional_factors) == optional
    assert set(profile.factor_precedence) == required | optional
    assert profile.token_budget == budget
    assert profile.latency_budget_ms == latency


def test_the_profiles_select_invariants_by_the_section_4_4_scopes() -> None:
    assert _resolve("session_start").scopes == ("process",)
    assert _resolve("agent_spawn").scopes == ("architecture",)


def test_selection_profiles_name_the_context_selection_task_class() -> None:
    assert _resolve("session_start").selection_task_class == "context_selection"
    assert _resolve("per_prompt").selection_task_class == "context_selection"
    assert _resolve("agent_spawn").selection_task_class is None


def _model_ids() -> set[str]:
    """Model and backend ids named by the repository's model routing data."""
    ids: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in {"bifrost_backend_id", "backend_id", "model_name"}
                    and isinstance(value, str)
                    and value
                ):
                    ids.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for path in (
        SRC / "model_policy.yaml",
        SRC / "configs" / "bifrost_delegation.yaml",
    ):
        walk(yaml.safe_load(path.read_text(encoding="utf-8")))
    return ids


# The overlay's repository names are private and never spelled in this public tree:
# the public-repo hygiene gate scans these files against its private denylist on
# every commit and in CI. This scan adds the name shape those repositories share.
_PRIVATE_REPOSITORY = re.compile(r"[a-z]+[_-](internal|private)\b")
_HOST = re.compile(
    r"\b\d{1,3}(\.\d{1,3}){3}\b|\blocalhost\b|\.local\b|\bh\d{3}\b|\.20[0-9]\b|:\d{4,5}\b"
)


def test_the_model_id_list_is_not_empty() -> None:
    """Positive control for the scan below: the routing data names model ids."""
    assert len(_model_ids()) >= 3


# The scan names the section 4.4 profiles as well as globbing the directory, so a
# section 4.4 profile that is missing fails here instead of being skipped.
@pytest.mark.parametrize("name", sorted(SECTION_4_4_PROFILES | set(PROFILE_NAMES)))
def test_no_profile_names_a_private_repository_a_host_or_a_model_id(
    name: str,
) -> None:
    text = (PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    assert not _PRIVATE_REPOSITORY.search(text)
    assert not _HOST.search(text)
    for model_id in _model_ids():
        assert not re.search(rf"(?<![\w.-]){re.escape(model_id)}(?![\w.-])", text), (
            model_id
        )


def test_the_scan_catches_a_planted_private_name_host_and_model_id() -> None:
    """Negative control: each pattern matches the kind of text it guards against."""
    assert _PRIVATE_REPOSITORY.search("source: example_internal")
    assert _HOST.search("endpoint: 203.0.113.7")
    assert _HOST.search("host: h101")
    model_id = sorted(_model_ids())[0]
    assert re.search(
        rf"(?<![\w.-]){re.escape(model_id)}(?![\w.-])", f"model: {model_id}\n"
    )


# --- window data (port item AP-B5) ---------------------------------------------


def _windowed(**overrides: Any) -> ModelContextProfile:
    values: dict[str, Any] = {
        "model_id": "fixture-consumer",
        "token_budget": 1000,
        "context_window_tokens": 10000,
        "reserved_output_tokens": 2000,
        "reserved_system_tokens": 3000,
    }
    values.update(overrides)
    return ModelContextProfile.model_validate(values)


def test_the_window_is_declared_minus_reserved_output_and_system() -> None:
    assert _windowed().effective_window_tokens() == 5000


@pytest.mark.parametrize(
    "missing",
    ["context_window_tokens", "reserved_output_tokens", "reserved_system_tokens"],
)
def test_a_window_without_its_reserved_shares_fails_validation(missing: str) -> None:
    with pytest.raises(ValueError, match="declared together"):
        _windowed(**{missing: None})


def test_a_window_that_cannot_hold_the_budget_fails_validation() -> None:
    with pytest.raises(ValueError, match="below token_budget"):
        _windowed(token_budget=5001)
    assert _windowed(token_budget=5000).token_budget == 5000


def test_the_window_has_no_default() -> None:
    """No host or model supplies a window: an undeclared window stays absent."""
    profile = ModelContextProfile(model_id="roi-runner")
    assert profile.context_window_tokens is None
    assert profile.effective_window_tokens() is None


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_every_shipped_profile_declares_a_window_that_holds_its_budget(
    name: str,
) -> None:
    profile = _resolve(name)
    effective = profile.effective_window_tokens()
    assert effective is not None
    assert effective >= profile.token_budget
