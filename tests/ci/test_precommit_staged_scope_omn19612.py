# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_contract_projection_check_compute.handlers.handler_projection_contract_check import (
    HandlerProjectionContractCheck,
)
from omnimarket.nodes.node_contract_projection_check_effect.handlers.handler_contract_projection_gather import (
    HandlerContractProjectionGather,
)
from omnimarket.nodes.node_contract_projection_check_effect.models import (
    ModelContractProjectionGatherRequest,
)
from omnimarket.validators.handler_event_type_source import scan_paths
from scripts.ci.check_aiokafka_construction_auth import main as aiokafka_main
from scripts.ci.check_watchdog_topic_authority import scan as scan_watchdog


def scan_projection_dlq(root: Path, paths: list[Path]) -> list[str]:
    """The DLQ rule over explicit paths, as pre-commit hands them to the node's runtime."""
    check_input = HandlerContractProjectionGather().handle(
        ModelContractProjectionGatherRequest(
            root=str(root), rule="dlq", filenames=tuple(str(p) for p in paths)
        )
    )
    report = HandlerProjectionContractCheck().handle(check_input)
    return [f.message for f in report.findings if f.rule_id == "projection-dlq-path"]


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
