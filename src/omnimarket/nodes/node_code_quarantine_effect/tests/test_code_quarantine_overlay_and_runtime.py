# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay, the contract and the command line of the quarantine node."""

from __future__ import annotations

import importlib
import json
import tomllib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_code_quarantine_effect import (
    HandlerCodeQuarantine,
    NodeCodeQuarantineEffect,
)
from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumQuarantineBackend,
    ModelQuarantineOverlay,
)
from omnimarket.nodes.node_code_quarantine_effect.overlay import (
    NODE_NAME,
    OVERLAY_FILE_ENV,
    load_overlay,
)
from omnimarket.nodes.node_code_quarantine_effect.runtime_code_quarantine import main
from omnimarket.nodes.node_code_quarantine_effect.tests.support import (
    quarantine_request,
)

pytestmark = pytest.mark.unit
_NODE = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[5]


def _example() -> dict[str, object]:
    loaded = yaml.safe_load((_NODE / "overlay.example.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_contract_models_handler_metadata_and_entry_point() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    metadata = yaml.safe_load((_NODE / "metadata.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"] == {"external_trigger": True}
    for key in ("input_model", "output_model"):
        module, name = contract[key].rsplit(".", 1)
        assert getattr(importlib.import_module(module), name)
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerCodeQuarantine
    )
    assert issubclass(NodeCodeQuarantineEffect, HandlerCodeQuarantine)
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    entry = pyproject["project"]["entry-points"]["onex.nodes"][NODE_NAME]
    assert entry == metadata["entry_points"]["onex.nodes"][NODE_NAME]


def test_example_overlay_loads_on_the_in_memory_backend() -> None:
    overlay = ModelQuarantineOverlay.model_validate(_example())
    assert overlay.backend is EnumQuarantineBackend.IN_MEMORY
    assert overlay.policy.quarantine_days == 7


def test_overlay_refuses_a_missing_policy_and_a_wrong_backend_block() -> None:
    without_policy = {k: v for k, v in _example().items() if k != "policy"}
    with pytest.raises(ValueError, match="policy"):
        ModelQuarantineOverlay.model_validate(without_policy)
    with pytest.raises(ValueError, match="files block"):
        ModelQuarantineOverlay.model_validate({**_example(), "backend": "files"})


def test_runtime_refuses_without_an_overlay(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(OVERLAY_FILE_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    request = _NODE / "overlay.example.yaml"
    assert main([str(request)]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_runtime_runs_a_request_on_the_example_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(_NODE / "overlay.example.yaml"))
    good = tmp_path / "request.json"
    payload = json.loads(quarantine_request("cli1").model_dump_json())
    payload.update(
        repository="example_repo",
        entity_paths=["src/example/unused_module.py"],
        test_paths=["tests/test_unused_module.py"],
    )
    good.write_text(json.dumps(payload))
    assert main([str(good)]) == 0
    assert "quarantined run=cli1 reading=WORKING" in capsys.readouterr().out
    payload["entity_paths"] = ["src/example/not_there.py"]
    good.write_text(json.dumps(payload))
    assert main([str(good)]) == 1
    assert "refused-unknown-path" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert main([str(bad)]) == 3


def test_load_overlay_reads_the_named_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(_NODE / "overlay.example.yaml"))
    assert load_overlay().backend is EnumQuarantineBackend.IN_MEMORY
