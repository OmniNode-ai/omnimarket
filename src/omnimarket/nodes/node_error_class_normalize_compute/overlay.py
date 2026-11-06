# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-side DI seam: snapshot error_normalizer configuration and host facts."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.error_normalizer import (
    ModelErrorNormalizerConfiguration,
    ModelErrorNormalizerFacts,
)

OVERLAY_ID = "error_normalizer"


def load_error_normalizer_facts(
    environ: Mapping[str, str] | None = None,
    *,
    explicit: object | None = None,
    hosts: tuple[str, ...] | None = None,
) -> ModelErrorNormalizerFacts:
    """Read once at the caller; retain these exact facts alongside key_version.

    The deployment assembler passes the host-list input from its lab-host
    overlay. No host discovery or runtime read occurs in the compute handler.
    An explicit snapshot also permits a replay with a previous host registry.
    """
    if isinstance(explicit, ModelErrorNormalizerFacts) or (
        isinstance(explicit, Mapping) and "configuration" in explicit
    ):
        return load_structured_overlay(
            OVERLAY_ID, ModelErrorNormalizerFacts, environ, explicit=explicit
        )
    configuration = load_structured_overlay(
        OVERLAY_ID, ModelErrorNormalizerConfiguration, environ, explicit=explicit
    )
    if hosts is None:
        raise NodeOverlayError(
            "error_normalizer requires the caller's lab-host overlay snapshot; "
            "supply hosts, or an explicit replay facts document"
        )
    return ModelErrorNormalizerFacts(configuration=configuration, hosts=hosts)
