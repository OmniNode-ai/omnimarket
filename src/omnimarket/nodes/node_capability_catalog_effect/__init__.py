# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capability catalog node: what the team has, collected and rendered from live sources.

``contract.yaml`` routes the ``collect_tools``, ``collect_lab``, ``render`` and ``publish`` operations
to the handlers in ``handlers/``, whose request and result types live in ``models/``. The
``onex-capability-catalog`` CLI is ``handlers/handler_capability_catalog_cli.py``.
"""

from __future__ import annotations

from .node import NodeCapabilityCatalogEffect

__all__ = ["NodeCapabilityCatalogEffect"]
