# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The regression node's contract-bound handlers are built by the runtime with no argument (OMN-26619).

Infra auto-wiring builds each handler a contract names with no constructor argument; a handler that requires ports is
quarantined, which under ``ONEX_WIRING_STRICT_MODE=1`` (the dev lane's setting) crash-loops the runtime. The ports are made
from the node's overlay on the first request instead, and a caller that passes every port gets exactly those ports.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.inmemory_adapters import (
    InMemoryChangeSource,
    InMemoryClaims,
    InMemoryRunSource,
    InMemorySeenStore,
    RecordingPublisher,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_delegation_check_regression import (
    HandlerDelegationCheckRegression,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_dev_build_record import (
    HandlerDevBuildRecord,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelRebuildCompletedObserved,
    ModelRegressionScanRequest,
)
from tests.unit.regression_support import Rig, settings

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_check_regression_effect"
)
OVERLAY_FILE_ENV = "ONEX_DELEGATION_CHECK_REGRESSION_OVERLAY"
NOW = datetime(2026, 11, 23, 14, 45, tzinfo=UTC)


def test_the_scan_handler_is_built_with_no_argument() -> None:
    assert HandlerDelegationCheckRegression() is not None


def test_the_dev_build_handler_is_built_with_no_argument() -> None:
    assert HandlerDevBuildRecord() is not None


def test_a_partial_set_of_ports_is_refused() -> None:
    with pytest.raises(ValueError, match="every port"):
        HandlerDelegationCheckRegression(
            settings=settings(), claims=InMemoryClaims({}), seen=InMemorySeenStore()
        )
    with pytest.raises(ValueError, match="both"):
        HandlerDevBuildRecord(runtime_lane="dev")


def test_the_scan_without_an_overlay_refuses_and_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(OVERLAY_FILE_ENV, raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        HandlerDelegationCheckRegression().handle(
            ModelRegressionScanRequest(now=NOW, write=False)
        )


def test_an_overlay_that_names_no_registry_root_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = yaml.safe_load((NODE / "overlay.example.yaml").read_text("utf-8"))
    document.pop("registry_root", None)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(overlay))
    with pytest.raises(NodeOverlayError, match="registry_root"):
        HandlerDelegationCheckRegression().handle(
            ModelRegressionScanRequest(now=NOW, write=False)
        )


def test_the_dev_build_handler_keeps_a_build_in_the_file_the_overlay_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = yaml.safe_load((NODE / "overlay.example.yaml").read_text("utf-8"))
    document["registry_root"] = str(tmp_path)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(overlay))
    request = ModelRebuildCompletedObserved(
        git_sha="a" * 40, completed_at=NOW, runtime_lane="dev"
    )
    recorded = HandlerDevBuildRecord().handle(request)
    assert recorded.recorded is True
    kept = tmp_path / ".onex_state/delegation-check-regression/dev-builds.jsonl"
    assert kept.is_file()
    assert "a" * 40 in kept.read_text("utf-8")


def test_explicit_ports_are_used_as_given() -> None:
    rig = Rig({})
    handler = HandlerDelegationCheckRegression(
        settings=rig.settings,
        runs=InMemoryRunSource({}),
        changes=InMemoryChangeSource(),
        claims=InMemoryClaims({}),
        seen=InMemorySeenStore(),
        publisher=RecordingPublisher(),
    )
    result = handler.handle(ModelRegressionScanRequest(now=NOW, write=False))
    assert result.events == ()
