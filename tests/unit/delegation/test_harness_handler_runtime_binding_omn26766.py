# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployed runtime binds the harness handler's two protocols (OMN-26766).

The runtime never calls ``bind_overlay``: only the host CLIs do (``--overlay``). The handler
the runtime auto-wires therefore met ``DependencyUnboundError`` for ProtocolProcessEnvironment
and ProtocolOverlayDocument. Here no overlay is installed, as in the runtime; the only
deployment facts are the process environment and ``ONEX_SKILL_OVERLAY_ROOTS``.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from omnimarket.handlers.container.container_overlay_dependencies import (
    OVERLAY_DOCUMENT_PROTOCOL,
    PROCESS_ENVIRONMENT_PROTOCOL,
    DependencyUnboundError,
    bind_overlay,
    declared_handler_dependencies,
    overlay_documents,
    process_environment,
    require_dependencies,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_delegation import (
    PACKAGE,
    HandlerHarnessDelegation,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def unbound_overlay() -> Generator[None, None, None]:
    bind_overlay(None)
    yield
    bind_overlay(None)


@pytest.fixture
def runtime_roots(
    unbound_overlay: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    tiers = tmp_path / "claude_harness_tiers"
    tiers.mkdir()
    (tiers / "overlay.yaml").write_text(
        "tiers:\n  harness-claude-sonnet:\n    credential_order: [subscription]\n"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv("OMN26766_PROBE", "present")
    return tmp_path


def test_the_runtime_binds_both_declared_protocols_with_no_installed_overlay(
    runtime_roots: Path,
) -> None:
    bound = require_dependencies(declared_handler_dependencies(PACKAGE))
    assert set(bound) == {PROCESS_ENVIRONMENT_PROTOCOL, OVERLAY_DOCUMENT_PROTOCOL}
    assert process_environment(bound).environment()["OMN26766_PROBE"] == "present"
    document = overlay_documents(bound).read("claude_harness_tiers")
    assert document is not None
    assert document["tiers"]["harness-claude-sonnet"]["credential_order"] == [
        "subscription"
    ]
    assert overlay_documents(bound).read("absent_document") is None


def test_the_handler_resolves_its_call_with_the_runtime_wiring(
    runtime_roots: Path,
) -> None:
    handler = HandlerHarnessDelegation()
    assert handler._bind() is not None


def test_no_roots_declared_still_refuses_before_anything_runs(
    unbound_overlay: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(DependencyUnboundError):
        require_dependencies(declared_handler_dependencies(PACKAGE))
