# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Codex model choice from the lane's scope, round, and explicit request."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_codex_model_compute.handlers import handler_codex_model as h
from omnimarket.nodes.node_codex_model_compute.models.model_codex_model import (
    ModelCodexModelRequest,
    ModelCodexModelResult,
)
from tests.node_overlay_support import install_node_overlay

DEFAULT = "default-model"
ESCALATION = "escalation-model"
EXPLICIT = "explicit-model"
DECLARED = (DEFAULT, ESCALATION, EXPLICIT)
POLICY = {
    "codex_models": list(DECLARED),
    "initial_model": DEFAULT,
    "escalation_model": ESCALATION,
    "escalation_round_threshold": 2,
}


@pytest.fixture(autouse=True)
def declared_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path / "declared",
        "node_codex_model_compute",
        POLICY,
    )


def _resolve(**facts: object) -> ModelCodexModelResult:
    return h.HandlerCodexModel().handle(ModelCodexModelRequest(**facts))


def test_no_model_asked_returns_the_overlay_default() -> None:
    result = _resolve()
    assert result.accepted is True
    assert result.model == DEFAULT
    assert result.declared == DECLARED


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_model_uses_the_overlay_default(blank: str) -> None:
    result = _resolve(codex_model=blank)
    assert result.accepted is True
    assert result.model == DEFAULT


@pytest.mark.parametrize(
    "facts",
    [
        {"security_scope": True, "round_count": 1},
        {"security_scope": False, "round_count": 2},
        {"security_scope": False, "round_count": 4},
    ],
)
def test_security_or_later_round_escalates(facts: dict[str, object]) -> None:
    result = _resolve(**facts)
    assert (result.accepted, result.model) == (True, ESCALATION)


@pytest.mark.parametrize("declared", DECLARED)
def test_declared_id_is_returned(declared: str) -> None:
    result = _resolve(codex_model=declared, security_scope=True, round_count=2)
    assert (result.accepted, result.model) == (True, declared)


@pytest.mark.parametrize(
    "undeclared", ["unknown-model", DEFAULT.upper(), f"{DEFAULT} ", "sonnet"]
)
def test_undeclared_id_is_refused_with_the_declared_ids_named(undeclared: str) -> None:
    result = _resolve(codex_model=undeclared, security_scope=True, round_count=2)
    assert result.accepted is False
    assert result.model is None
    assert all(model in result.reason for model in DECLARED)
    assert (
        repr(undeclared.strip()) in result.reason or undeclared.strip() in result.reason
    )


def test_overlay_sets_both_roles_and_the_round_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_codex_model_compute",
        {
            "codex_models": ["model-a", "model-b"],
            "initial_model": "model-b",
            "escalation_model": "model-a",
            "escalation_round_threshold": 3,
        },
    )
    assert _resolve(round_count=2).model == "model-b"
    assert _resolve(round_count=3).model == "model-a"
    refused = _resolve(codex_model=DEFAULT)
    assert refused.accepted is False
    assert refused.declared == ("model-a", "model-b")


@pytest.mark.parametrize(
    ("brief", "security", "round_count", "model"),
    [
        ("# Task\n", False, 1, DEFAULT),
        ("Security scope: no\nRound: 1\n", False, 1, DEFAULT),
        ("Security scope: yes\n", True, 1, ESCALATION),
        ("Security scope: no\nRound: 2\n", False, 2, ESCALATION),
        (
            "\x60\x60\x60md\nSecurity scope: yes\nRound: 3\n\x60\x60\x60\n> Round: 4\n",
            False,
            1,
            DEFAULT,
        ),
    ],
)
def test_brief_metadata_populates_typed_fields(
    brief: str, security: bool, round_count: int, model: str
) -> None:
    request = ModelCodexModelRequest(brief=brief)
    assert request.security_scope is security
    assert request.round_count == round_count
    assert h.HandlerCodexModel().handle(request).model == model


def test_explicit_scope_and_round_override_the_brief() -> None:
    assert (
        _resolve(
            brief="Security scope: yes\nRound: 2", security_scope=False, round_count=1
        ).model
        == DEFAULT
    )


def test_no_overlay_refuses_instead_of_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    with pytest.raises(NodeOverlayNotFoundError):
        h.HandlerCodexModel()


def test_broken_overlay_refuses_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "node_codex_model_compute", POLICY | {"codex_models": []}
    )
    with pytest.raises(NodeOverlayError):
        h.HandlerCodexModel()


@pytest.mark.parametrize(
    "field", [{"model": DEFAULT}, {"round_count": 0}, {"security_scope": "perhaps"}]
)
def test_request_rejects_invalid_fields(field: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelCodexModelRequest.model_validate(field)


@pytest.mark.parametrize("key", ["initial_model", "escalation_model"])
def test_overlay_refuses_a_role_outside_the_declared_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_codex_model_compute",
        POLICY | {key: "unknown-model"},
    )
    with pytest.raises(NodeOverlayError):
        h.HandlerCodexModel()


def test_contract_binding_and_installed_entry_point() -> None:
    contract = yaml.safe_load(h.CONTRACT_PATH.read_text())
    binding = contract["handler"]
    cls = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert contract["overlay"].keys() == POLICY.keys()
    assert not any(m in h.CONTRACT_PATH.read_text() for m in DECLARED)
    request = ModelCodexModelRequest(round_count=2)
    result = cls().handle(request)
    entry = next(
        e for e in entry_points(group="onex.nodes") if e.name == contract["name"]
    )
    node = entry.load().NodeCodexModelCompute
    assert node().handle(request) == result
    metadata = yaml.safe_load((h.CONTRACT_PATH.parent / "metadata.yaml").read_text())
    assert metadata["name"] == contract["name"]
