# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topic literal rule of the contract topic reader, read only from the deployment overlay."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_contract_topics import ModelContractTopicRule

NODE_NAME = "node_contract_topic_reader_compute"


def load_contract_topic_rule(
    environ: Mapping[str, str] | None = None,
) -> ModelContractTopicRule:
    """Read the pattern a topic literal must fully match.

    The rule is a deployment fact: ``<root>/node_contract_topic_reader_compute/overlay.yaml``
    under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. There is no packaged fallback: when no
    overlay exists the loader refuses with ``NodeOverlayNotFoundError`` rather than read topics
    with a guessed pattern.
    """
    return load_structured_overlay(NODE_NAME, ModelContractTopicRule, environ)
