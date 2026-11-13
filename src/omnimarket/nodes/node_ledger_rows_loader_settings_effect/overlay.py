# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlays this node reads: every deployment fact, none in code.

Found at ``<root>/<overlay id>/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS`` (the roots
the one overlay root resolver answers, through ``node_overlay_reader.find_node_overlay``). The loader overlay is
``node_ledger_rows_loader_effect`` (the file fallback flag, the journal variable); the hosts overlay is
``ledger_rows_loader_hosts`` (the per-host loader command and its bound). No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    ModelRowsLoaderOverlay,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.overlay import (
    load_overlay as load_loader_overlay_document,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    ModelLoaderHostsOverlay,
)

HOSTS_OVERLAY_ID = "ledger_rows_loader_hosts"


def load_loader_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelRowsLoaderOverlay:
    return load_loader_overlay_document(environ)


def load_hosts_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLoaderHostsOverlay:
    return load_structured_overlay(HOSTS_OVERLAY_ID, ModelLoaderHostsOverlay, environ)
