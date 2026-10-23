# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Faster cold emit parsing preserves policy data and rejects Python tags."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_event_emit_effect import redaction
from omnimarket.nodes.node_event_emit_effect.spool import topic_resolver

pytestmark = pytest.mark.unit


def test_topic_registry_matches_python_safe_loader() -> None:
    path = topic_resolver.default_registry_path()
    expected = topic_resolver._parse_registry(
        yaml.safe_load(path.read_text(encoding="utf-8")), source=path
    )
    assert topic_resolver._load_registry(str(path)) == expected


def test_redaction_policy_matches_python_safe_loader() -> None:
    path = redaction.default_contract_path()
    expected = redaction._parse(
        yaml.safe_load(path.read_text(encoding="utf-8")), source=path
    )
    assert redaction.load_contract(path) == expected


@pytest.mark.parametrize("python_loader", [False, True])
def test_emit_loaders_refuse_python_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_loader: bool
) -> None:
    if python_loader:
        monkeypatch.setattr(topic_resolver, "_REGISTRY_SAFE_LOADER", yaml.SafeLoader)
        monkeypatch.setattr(redaction, "_CONTRACT_SAFE_LOADER", yaml.SafeLoader)
    path = tmp_path / "unsafe.yaml"
    path.write_text("!!python/tuple [1, 2]\n", encoding="utf-8")
    with pytest.raises(yaml.constructor.ConstructorError):
        topic_resolver._load_registry(str(path))
    with pytest.raises(yaml.constructor.ConstructorError):
        redaction.load_contract(path)
