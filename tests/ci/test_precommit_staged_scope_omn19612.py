# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.validators.handler_event_type_source import scan_paths
from scripts.ci.check_aiokafka_construction_auth import main as aiokafka_main
from scripts.ci.check_projection_dlq_path import _scan as scan_projection_dlq
from scripts.ci.check_watchdog_topic_authority import scan as scan_watchdog


@pytest.mark.unit  # type: ignore[untyped-decorator]
def test_handler_event_type_guard_scopes_to_selected_files(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    dirty = tmp_path / "dirty.py"
    clean.write_text("event_type = message.event_type\n", encoding="utf-8")
    dirty.write_text(
        'if message.event_type == "onex.evt.x.y.v1":\n    pass\n', encoding="utf-8"
    )

    assert scan_paths([clean])[0] == []
    assert scan_paths([dirty])[0]


@pytest.mark.unit  # type: ignore[untyped-decorator]
def test_aiokafka_guard_scopes_to_selected_files(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    dirty = tmp_path / "dirty.py"
    clean.write_text("value = 1\n", encoding="utf-8")
    dirty.write_text("client = AIOKafkaProducer()\n", encoding="utf-8")

    assert aiokafka_main([str(clean)]) == 0
    assert aiokafka_main([str(dirty)]) == 1


@pytest.mark.unit  # type: ignore[untyped-decorator]
def test_projection_dlq_guard_scopes_to_selected_files(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    dirty = tmp_path / "dirty.py"
    clean.write_text("value = 1\n", encoding="utf-8")
    dirty.write_text("except ValidationError:\n    pass\n", encoding="utf-8")

    assert scan_projection_dlq(tmp_path, [clean]) == []
    assert scan_projection_dlq(tmp_path, [dirty])


@pytest.mark.unit  # type: ignore[untyped-decorator]
def test_watchdog_guard_scopes_to_selected_files(tmp_path: Path) -> None:
    src = tmp_path / "src" / "omnimarket"
    src.mkdir(parents=True)
    clean = src / "clean.py"
    dirty = src / "dirty.py"
    clean.write_text("value = 1\n", encoding="utf-8")
    dirty.write_text('TOPIC = "onex.evt.x.workflow-stalled.v1"\n', encoding="utf-8")

    assert scan_watchdog(tmp_path, [clean]) == []
    assert scan_watchdog(tmp_path, [dirty])
