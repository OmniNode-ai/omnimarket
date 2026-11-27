# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the outbox flush and its entry: every deployment fact is found by reference, nothing has a default."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_tenant_outbox_flush_effect import overlay as overlay_module
from omnimarket.nodes.node_tenant_outbox_flush_effect.overlay import (
    ModelTenantOutboxFlushOverlay,
    build_flush,
    load_overlay,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.runtime_tenant_outbox_flush import (
    main,
)

pytestmark = pytest.mark.unit

EXAMPLE = Path(overlay_module.__file__).with_name("overlay.example.yaml")


def _document() -> dict[str, object]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _root(tmp_path: Path, document: dict[str, object]) -> dict[str, str]:
    node = tmp_path / "node_tenant_outbox_flush_effect"
    node.mkdir()
    (node / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_the_example_validates_and_names_a_kafka_lane() -> None:
    overlay = ModelTenantOutboxFlushOverlay.model_validate(_document())
    assert overlay.event_bus.type == "kafka"
    assert overlay.database.dsn_secret == "tenant_identity.database.dsn"


@pytest.mark.parametrize("missing", ["lane", "database", "event_bus", "batch_limit"])
def test_a_key_the_overlay_leaves_out_is_refused(missing: str) -> None:
    document = _document()
    del document[missing]
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushOverlay.model_validate(document)


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushOverlay.model_validate({**_document(), "extra": 1})


def test_the_overlay_is_found_under_the_declared_root(tmp_path: Path) -> None:
    environ = _root(tmp_path, _document())
    assert load_overlay(environ).lane == "example-lane"


def test_build_flush_connects_to_nothing(tmp_path: Path) -> None:
    overlay = ModelTenantOutboxFlushOverlay.model_validate(_document())
    handler, closers = build_flush(overlay, topic="onex.tenant.events")
    assert handler is not None
    assert len(closers) == 2


def test_probe_prints_the_lane_without_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in _root(tmp_path, _document()).items():
        monkeypatch.setenv(name, value)
    assert main(["--probe"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"probe": "ready", "lane": "example-lane", "batch_limit": 50}


def test_a_missing_overlay_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert main(["--probe"]) == 3


def test_without_a_mode_the_entry_is_indeterminate() -> None:
    assert main([]) == 3
