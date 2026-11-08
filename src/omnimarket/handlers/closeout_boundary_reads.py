# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two reads the closeout sweep's compute node takes from outside it (OMN-20655).

node_dod_closeout_sweep_compute is a compute node and does no file or environment I/O. Its
command line and its watcher-state reader take these two reads from here, so the node's own
files stay free of I/O and of suppression comments.
"""

from __future__ import annotations

import os
from pathlib import Path


def read_text_file(path: Path) -> str:
    """The text of ``path``; raises OSError when it cannot be read."""
    return path.read_text(encoding="utf-8")


def registry_root_from_environment() -> str:
    """The registry root OMNI_HOME names, or an empty string when it is unset."""
    return os.environ.get("OMNI_HOME", "")
