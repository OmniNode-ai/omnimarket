# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The never-produced listing rule, read only from the deployment overlay."""

from collections.abc import Mapping

from omnimarket.events.topic_never_produced import (
    ModelNeverProducedRule,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_topic_never_produced_compute"


def load_never_produced_rule(
    environ: Mapping[str, str] | None = None,
) -> ModelNeverProducedRule:
    """Read the protected prefixes and the declared-producer listing policy.

    The rule is a deployment fact: ``<root>/node_topic_never_produced_compute/overlay.yaml``
    under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. There is no packaged fallback:
    when no overlay exists the loader refuses with ``NodeOverlayNotFoundError`` rather
    than list broker-internal topics with guessed prefixes.
    """
    return load_structured_overlay(NODE_NAME, ModelNeverProducedRule, environ)
