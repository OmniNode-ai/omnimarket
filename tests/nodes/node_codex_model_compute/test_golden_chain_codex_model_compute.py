# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Codex model choice: no model by default, a declared id when asked, a refusal otherwise."""

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

DECLARED = ("gpt-6.2-sol", "gpt-6.2-astra")


@pytest.fixture(autouse=True)
def declared_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path / "declared",
        "node_codex_model_compute",
        {"codex_models": list(DECLARED)},
    )


def _resolve(**facts: object) -> ModelCodexModelResult:
    return h.HandlerCodexModel().handle(ModelCodexModelRequest(**facts))


def test_no_model_asked_returns_no_model_the_account_default() -> None:
    result = _resolve()
    assert result.accepted is True
    assert result.model is None
    assert result.declared == DECLARED


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_model_is_the_account_default(blank: str) -> None:
    result = _resolve(codex_model=blank)
    assert result.accepted is True
    assert result.model is None


@pytest.mark.parametrize("declared", DECLARED)
def test_declared_id_is_returned(declared: str) -> None:
    result = _resolve(codex_model=declared)
    assert (result.accepted, result.model) == (True, declared)


@pytest.mark.parametrize(
    "undeclared", ["gpt-5.6-sol", "GPT-6.2-SOL", "gpt-6.2-sol ", "gpt-6.2", "sonnet"]
)
def test_undeclared_id_is_refused_with_the_declared_ids_named(undeclared: str) -> None:
    result = _resolve(codex_model=undeclared)
    assert result.accepted is False
    assert result.model is None
    assert all(model in result.reason for model in DECLARED)
    assert (
        repr(undeclared.strip()) in result.reason or undeclared.strip() in result.reason
    )


def test_overlay_replaces_the_declared_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "node_codex_model_compute", {"codex_models": ["gpt-x"]}
    )
    handler = h.HandlerCodexModel()
    assert handler.handle(ModelCodexModelRequest(codex_model="gpt-x")).model == "gpt-x"
    refused = handler.handle(ModelCodexModelRequest(codex_model="gpt-6.2-sol"))
    assert refused.accepted is False
    assert refused.declared == ("gpt-x",)


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
        monkeypatch, tmp_path, "node_codex_model_compute", {"codex_models": []}
    )
    with pytest.raises(NodeOverlayError):
        h.HandlerCodexModel()


def test_request_rejects_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        ModelCodexModelRequest(model="gpt-6.2-sol")  # type: ignore[call-arg]


def test_contract_binding_and_installed_entry_point() -> None:
    contract = yaml.safe_load(h.CONTRACT_PATH.read_text())
    binding = contract["handler"]
    cls = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert "codex_models" in contract["overlay"]
    assert not any(m in h.CONTRACT_PATH.read_text() for m in DECLARED)
    request = ModelCodexModelRequest(codex_model="gpt-6.2-astra")
    result = cls().handle(request)
    entry = next(
        e for e in entry_points(group="onex.nodes") if e.name == contract["name"]
    )
    node = entry.load().NodeCodexModelCompute
    assert node().handle(request) == result
    metadata = yaml.safe_load((h.CONTRACT_PATH.parent / "metadata.yaml").read_text())
    assert metadata["name"] == contract["name"]
