# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The workspace onex setup makes is one onex delegate can use (OMN-23413).

Reported 2026-11-13T01:41Z (coordination MSG 7fe795c4, from re-verifying the
local dev guide) and reproduced here the same day: after `onex setup --path
local` succeeds, `OMNIBASE_PATH=<ws> onex delegate` exits 1 with "workspace
root ... is bound but has no materialised runtime config ... and declares no
working-tree runtime config". Setup wrote <ws>/config/onex/runtime/, while
onex delegate reads a developer workspace's config under <ws>/.onex/
workspace-config (omnibase_infra's HandlerWorkspaceConfigRootResolve,
OMN-20700). With the file copied there, the same delegate answered.

Failure modes:
  W1  setup writes the runtime config where onex delegate does not look.
  W2  setup's own check of the step reads a different place than it writes,
      so a re-run reports the step missing or rewrites it every time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.handlers.handler_workspace_config_root_resolve import (
    HandlerWorkspaceConfigRootResolve,
)
from omnibase_infra.models.workspace import ModelWorkspaceConfigRootRequest

from omnimarket.nodes.node_onex_setup_effect.handlers import handler_onex_setup

pytestmark = pytest.mark.unit


def test_onex_delegate_finds_the_runtime_config_setup_wrote(tmp_path: Path) -> None:
    """W1: judged by the resolver onex delegate itself uses."""
    written, changed = handler_onex_setup.ensure_runtime_config(tmp_path, "local")
    assert changed
    resolved = HandlerWorkspaceConfigRootResolve().handle(
        ModelWorkspaceConfigRootRequest(workspace_root=tmp_path, environment={})
    )
    assert resolved.source == "developer"
    assert resolved.config_root is not None
    found = resolved.config_root / "config/onex/runtime/runtime_config.yaml"
    assert found.resolve() == written.resolve()
    assert found.read_text().startswith(handler_onex_setup.SETUP_MARKER)


def test_a_second_run_leaves_the_written_config_alone(tmp_path: Path) -> None:
    """W2."""
    first, changed = handler_onex_setup.ensure_runtime_config(tmp_path, "local")
    again, changed_again = handler_onex_setup.ensure_runtime_config(tmp_path, "local")
    assert changed
    assert not changed_again
    assert first == again
