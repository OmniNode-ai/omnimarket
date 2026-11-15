# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Removing the last local model leaves every other model routable (OMN-23413).

Found 2026-11-13 from the operator's /config: Reflection AI's key was accepted
and its model added, but its test failed with "Bifrost delegation config schema
validation failed: backends Tuple should have at least 1 item ... not 0". The
routes file the TUI's home held was the single line `backends:`, written when
its last local model was removed. YAML reads that as null, and the loader's
deep merge put the null over the contract's backend list, so no backend at all
was left and every delegation test failed.

Failure modes:
  E1  removing the last local model writes a routes file that empties routing.
  E2  a routes file already holding a bare `backends:` empties routing.
  E3  delegate's own backend resolution fails on that file ("'NoneType' object
      is not iterable").
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup

pytestmark = pytest.mark.unit


def test_removing_the_last_local_model_writes_no_null_backends() -> None:
    """E1: what the remove path writes, read back as YAML."""
    one = handler_model_setup._with_local_urls(
        "",
        [
            {
                "backend_id": handler_model_setup.local_url_backend_id(1),
                "endpoint_url": "http://models.test:8000/v1/chat/completions",
                "model_name": "m",
                **handler_model_setup._LOCAL_URL_DEFAULTS,
            }
        ],
    )
    emptied = handler_model_setup._with_local_urls(
        handler_model_setup._without_local_url(one), []
    )
    data = yaml.safe_load(emptied) or {}
    assert data.get("backends", []) is not None


def test_a_bare_backends_line_keeps_the_contracts_backends(tmp_path: Path) -> None:
    """E2: the file the operator's home already holds."""
    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text("backends:\n")
    config = load_bifrost_delegation_config(overlay_path=overlay)
    assert len(config.backends) > 0


def test_delegate_routing_reads_a_bare_backends_line(tmp_path: Path) -> None:
    """E3: `onex delegate` itself, not only the bifrost loader, reads the file.

    Seen 2026-11-13 in the operator's TUI: with the same `backends:` file every
    typed task failed "delegation failed: 'NoneType' object is not iterable"
    (run 618e3dc2), from `list(overlay.get("backends", []))`: the default
    applies to a missing key, not to a null one.
    """
    from omnimarket.handlers.routing.delegation_backend_resolution import (
        load_bifrost_backends,
    )

    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text("backends:\n")
    assert len(load_bifrost_backends(overlay_path=overlay)) > 0
