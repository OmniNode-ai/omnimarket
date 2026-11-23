# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publish command's selection values, read from the selector's policy file the stage gate reads."""

from __future__ import annotations

from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import (
    load_policy,
    policy_version,
    selector_version,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    ModelRecordSettings,
)


class SelectionPolicyRecordSettings:
    """``stage_entry_counted`` is the policy's ``selection_stage``; the versions are the selector's own."""

    def read(self) -> ModelRecordSettings:
        try:
            return ModelRecordSettings(
                selector_version=selector_version(),
                policy_version=policy_version(),
                stage_entry_counted=load_policy().selection_stage,
            )
        except (OSError, ValueError, KeyError, ValidationError) as exc:
            raise ValueError(
                f"the selector's policy could not be read ({type(exc).__name__})"
            ) from exc


__all__: list[str] = ["SelectionPolicyRecordSettings"]
