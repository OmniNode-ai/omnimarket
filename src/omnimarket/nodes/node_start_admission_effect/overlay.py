# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``start_admission`` overlay: every setting of admission, none in code.

Shape only; which start paths exist (the census), the mode of each and the log backend belong to
the deployment owner and are read from ``<root>/start_admission/overlay.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_start_admission import ModelStartAdmissionPolicy

OVERLAY_ID = "start_admission"


def load_start_admission_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelStartAdmissionPolicy:
    """Load and validate the overlay; an absent or unknown key refuses and names itself."""
    return load_structured_overlay(OVERLAY_ID, ModelStartAdmissionPolicy, environ)
