# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_transcript_session_scan_effect declares its topics, overlay keys and store dependency; hardcodes none (OMN-22259)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_transcript_session_scan_effect.models import (
    ModelTranscriptScanOverlay,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_transcript_session_scan_effect"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
COMMAND = "onex.cmd.omnimarket.transcript-session-scan-requested.v1"
EVENT = "onex.evt.omnimarket.transcript-session-scanned.v1"


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def test_the_contract_declares_its_command_and_terminal_event() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [EVENT]
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND
    assert contract["terminal_event"] == EVENT


def test_the_contract_handler_resolves_to_an_effect_class_with_handle() -> None:
    handler = _contract()["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert callable(cls.handle)
    assert _contract()["node_type"] == "EFFECT_GENERIC"
    assert _contract()["descriptor"]["node_archetype"] == "effect"


def test_the_overlay_fields_are_exactly_the_three_keys_of_the_overlay_model() -> None:
    declared = _contract()["overlay"]["fields"]
    assert declared == ["transcript_roots", "manifest_hash_algorithm", "read_max_bytes"]
    assert sorted(declared) == sorted(ModelTranscriptScanOverlay.model_fields)


def test_the_store_is_a_declared_required_protocol_dependency_on_the_spi_port() -> None:
    [dependency] = _contract()["protocol_dependencies"]
    assert dependency["protocol"].startswith("omnibase_spi.protocols.storage.")
    assert dependency["required"] is True
    module, _, name = dependency["protocol"].partition(":")
    assert hasattr(importlib.import_module(module), name)


def test_the_node_is_registered_as_an_onex_node_entry_point() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entries[NODE] == f"omnimarket.nodes.{NODE}"


def test_the_package_hardcodes_no_topic_root_or_envelope_and_calls_no_model() -> None:
    sources = list(NODE_DIR.rglob("*.py"))
    assert sources
    for path in sources:
        body = path.read_text(encoding="utf-8")
        assert "ModelEventEnvelope" not in body, path
        assert "onex.cmd." not in body, path
        assert "onex.evt." not in body, path
        assert "s3://" not in body, path
        assert "subprocess" not in body, path
        assert "anthropic" not in body.lower(), path
