# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``release_gate`` overlay of the release gate nodes (OMN-21493).

Shape only; the values (the grant label, the role lists by login, the agent
identities, the train and tagger identities, the release shapes) belong to the
deployment owner and live in the private overlay. The caller loads the overlay once
and passes the resulting policy to the nodes; a handler never reads it.

The gate evaluates the base side and the private overlay. A root of
``ONEX_SKILL_OVERLAY_ROOTS`` that lies inside the pull request's head checkout is
never read, so a pull request cannot supply its own policy by adding an overlay file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    load_structured_overlay,
)
from omnimarket.models.release_gate.model_release_gate_policy import (
    ModelReleaseGatePolicy,
)

OVERLAY_ID = "release_gate"


def _outside(root: str, head_checkout: Path) -> bool:
    try:
        Path(root).resolve().relative_to(head_checkout.resolve())
    except ValueError:
        return True
    return False


def load_release_gate_policy(
    environ: Mapping[str, str] | None = None,
    *,
    head_checkout: Path | None = None,
) -> ModelReleaseGatePolicy:
    """Load and validate the ``release_gate`` overlay; refuses naming the missing key.

    With ``head_checkout`` set, every overlay root inside that checkout is dropped
    before the search, so no file the pull request carries is read.
    """
    env = dict(os.environ if environ is None else environ)
    if head_checkout is not None:
        roots = [r for r in env.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep) if r]
        env[OVERLAY_ROOTS_ENV] = os.pathsep.join(
            r for r in roots if _outside(r, head_checkout)
        )
    return load_structured_overlay(OVERLAY_ID, ModelReleaseGatePolicy, env)
