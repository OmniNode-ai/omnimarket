# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The minimum live topic listing of the required-and-absent reading, read only from the deployment overlay."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_topic_required_absent import ModelRequiredAbsentRule

NODE_NAME = "node_topic_required_absent_compute"


def load_required_absent_rule(
    environ: Mapping[str, str] | None = None,
) -> ModelRequiredAbsentRule:
    """Read the smallest live topic listing that counts as a read.

    The rule is a deployment fact: ``<root>/node_topic_required_absent_compute/overlay.yaml``
    under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. There is no packaged fallback: when no
    overlay exists the loader refuses with ``NodeOverlayNotFoundError`` rather than judge topics
    against a guessed minimum.
    """
    return load_structured_overlay(NODE_NAME, ModelRequiredAbsentRule, environ)
