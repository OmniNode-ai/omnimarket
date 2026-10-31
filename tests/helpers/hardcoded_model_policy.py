# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hardcoded-model-config policy as omnibase_core's own runtime resolves it.

omnibase_core reads the private lab denylist through its private-vocabulary
composition root (OMN-21494). Where no private vocabulary resolves, as on a
public CI runner, the policy carries the shape rules and no retired values.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnibase_core.handlers.handler_private_vocabulary_resolution import (
    build_container,
    capture_environment,
    resolve_private_vocabulary,
)
from omnibase_core.validation.hardcoded_model_config.models import (
    ModelHardcodedModelConfigPolicy,
)
from omnibase_core.validation.hardcoded_model_config.runtime_hardcoded_model_config import (
    load_policy,
)

_VOCABULARY_NAME = "hardcoded_model_config_lab_denylist"


def resolved_policy() -> ModelHardcodedModelConfigPolicy:
    container = build_container()
    vocabulary = resolve_private_vocabulary(
        container,
        _VOCABULARY_NAME,
        environment=capture_environment(container),
        now=datetime.now(UTC),
    )
    return load_policy(vocabulary)
