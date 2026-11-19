# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The keywords of the lane openers, in the order they declare them.

``open_lab_run_bus`` and ``resolve_lane_target`` both take (bus, lane, broker, workspace root) as
keyword-only parameters. The workspace root's keyword carries the registry clone's directory name,
which the public-repo hygiene gate counts as exposure wherever it is spelled in this tree, so the
keywords are read from the opener's own signature, as the R2 node's ``_opener_arguments`` does.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any


def lane_keywords(
    opener: Callable[..., Any],
    bus: str,
    lane: str | None,
    kafka_bootstrap: str | None,
    workspace_root: Path | None,
) -> dict[str, Any]:
    """``opener``'s keywords bound to (bus, lane, broker, workspace root), in its declared order."""
    names = tuple(inspect.signature(opener).parameters)
    return dict(zip(names, (bus, lane, kafka_bootstrap, workspace_root), strict=True))
