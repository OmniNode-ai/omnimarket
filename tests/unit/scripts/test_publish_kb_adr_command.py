# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Contract tests for the KB ADR publish command payload."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

SCRIPT_PATH = Path(__file__).parents[3] / "scripts" / "publish_kb_adr_command.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("publish_kb_adr_command", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**overrides: object) -> argparse.Namespace:
    payload: dict[str, object] = {
        "canary_run_dir": ".onex_state/adr-canary-runs/run-1",
        "model_key": "test/qwen3",
        "kb_destination": "private",
        "source_repository": "OmniNode-ai/omni_home",
        "source_visibility": "private",
        "publication_classification": "restricted",
        "dry_run": True,
    }
    payload.update(overrides)
    return argparse.Namespace(**payload)


def test_publish_command_builds_exact_typed_request() -> None:
    module = _load_script_module()

    request = module._build_publish_request(_args())

    assert request.kb_destination.value == "private"
    assert request.source_provenance.source_repository == "OmniNode-ai/omni_home"
    assert request.source_provenance.publication_classification.value == "restricted"
    assert "kb_repo" not in request.model_dump(mode="json")


def test_publish_command_rejects_conflicting_explicit_classification() -> None:
    module = _load_script_module()

    with pytest.raises(ValidationError, match="private source visibility conflicts"):
        module._build_publish_request(_args(publication_classification="public"))


def test_publish_command_does_not_accept_arbitrary_kb_repo(monkeypatch) -> None:
    module = _load_script_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish_kb_adr_command.py",
            "--canary-run-dir",
            ".onex_state/adr-canary-runs/run-1",
            "--model-key",
            "test/qwen3",
            "--kb-repo",
            "attacker/knowledge-base",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        module._parse_args()
