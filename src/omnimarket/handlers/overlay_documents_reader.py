# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File reads for the undeclared read check's command line (OMN-26424).

The check node judges a diff and overlay documents it is handed and reads no file. The command line
that hands them over reads them here, beside ``node_overlay_reader``, so the node stays free of I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

OVERLAY_FILE_NAME = "overlay.yaml"


def read_text_file(path: str) -> str:
    """The text of one file: the unified diff the check judges."""
    return Path(path).read_text()


def load_overlay_documents(roots: list[str]) -> dict[str, Any]:
    """Every ``<root>/<name>/overlay.yaml`` document, keyed by ``<name>``; the first root holding a name wins."""
    documents: dict[str, Any] = {}
    for root in roots:
        for path in sorted(Path(root).glob(f"*/{OVERLAY_FILE_NAME}")):
            documents.setdefault(path.parent.name, yaml.safe_load(path.read_text()))
    return documents
