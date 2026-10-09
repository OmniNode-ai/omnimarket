# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Kinds of deployment fact a packaged routing config field can carry (OMN-20287).

A deployment fact is a choice one deployment made about where and on what its
delegation runs: which backends exist, their endpoint hosts and ports, the model
names they serve, the secret references they authenticate with, the providers
they are bought from, and the per-class order they are tried in. Those choices
belong to the deployment's overlay, not to the config this package ships to
every customer.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDeploymentFactKind(StrEnum):
    """What a field marked as a deployment fact records."""

    BACKEND = "backend"
    """Which backend exists, by its id."""

    PROVIDER = "provider"
    """Which provider a backend is bought from."""

    ENDPOINT = "endpoint"
    """The endpoint host and port a backend is reached on."""

    MODEL_NAME = "model_name"
    """The model a backend or tier serves."""

    SECRET_REF = "secret_ref"
    """The credential reference a backend authenticates with."""

    ROUTING_TIER = "routing_tier"
    """Which routing tier exists, by its name."""

    ROUTING_ORDER = "routing_order"
    """The ordered backends or tiers a task class, rule or default tries."""


__all__: list[str] = ["EnumDeploymentFactKind"]
