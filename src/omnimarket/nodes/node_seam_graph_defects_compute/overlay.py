# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam graph rule of the defects compute, read only from the deployment overlay."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_seam_graph_defects import ModelSeamGraphRule

NODE_NAME = "node_seam_graph_defects_compute"


def load_seam_graph_rule(
    environ: Mapping[str, str] | None = None,
) -> ModelSeamGraphRule:
    """Read the rule driving the whole seam graph scan.

    The rule is a deployment fact: ``<root>/node_seam_graph_defects_compute/overlay.yaml``
    under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. There is no packaged fallback:
    when no overlay exists the loader refuses with ``NodeOverlayNotFoundError`` rather
    than scan the repos with guessed patterns and markers.
    """
    return load_structured_overlay(NODE_NAME, ModelSeamGraphRule, environ)
