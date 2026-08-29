# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How often the channel must prove itself, read from the contract (OMN-15600).

There are no defaults here, for the same reason OMN-16778 has none: an interval
a code default can satisfy is an interval nobody can change without a deploy,
and ``test_no_interval_literal_lives_in_python`` asserts mechanically that no
bare integer stands in for this block anywhere under the node.

The loader fails closed.  A liveness checker that silently substitutes its own
cadence when its contract is unreadable is a checker whose schedule is not the
schedule anyone declared -- and an alert-channel probe nobody scheduled is the
on-demand test-fire this ticket was filed to replace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_POLICY_KEY = "liveness_policy"


class ModelAlertChannelLivenessPolicy(BaseModel):
    """Contract-declared cadence for the alert-channel probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_interval_seconds: int = Field(
        ...,
        ge=1,
        description=(
            "Minimum seconds between two probes. The heartbeat that triggers "
            "this node ticks far more often than the channel needs proving, "
            "so ticks inside an interval already proven are skipped rather "
            "than re-probed. This is a throttle on an existing schedule, not a "
            "schedule of its own -- the node owns no timer and no loop."
        ),
    )
    probe_timeout_seconds: float = Field(
        ...,
        gt=0,
        description=(
            "Per-request ceiling for a read-only Slack probe. A probe that "
            "hangs must become PROBE_ERROR on a bounded clock rather than "
            "block the heartbeat consumer indefinitely."
        ),
    )


class AlertChannelLivenessPolicyError(RuntimeError):
    """The contract does not declare a usable ``liveness_policy`` block."""


def load_liveness_policy(contract_path: Path) -> ModelAlertChannelLivenessPolicy:
    """Read the ``liveness_policy`` block out of the node contract.

    Args:
        contract_path: Contract to read. Passed in rather than resolved from a
            module constant so a test can point the node at a modified copy and
            watch the cadence change with no code edit.

    Returns:
        The parsed, fully-required policy.

    Raises:
        AlertChannelLivenessPolicyError: The file is missing, unparseable,
            declares no ``liveness_policy`` block, or declares one that does
            not validate.
    """
    try:
        raw: Any = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AlertChannelLivenessPolicyError(
            f"cannot read alert-channel liveness contract at {contract_path}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise AlertChannelLivenessPolicyError(
            f"alert-channel liveness contract at {contract_path} is not valid "
            f"YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise AlertChannelLivenessPolicyError(
            f"alert-channel liveness contract at {contract_path} is not a mapping"
        )
    block = raw.get(_POLICY_KEY)
    if not isinstance(block, dict):
        raise AlertChannelLivenessPolicyError(
            f"alert-channel liveness contract at {contract_path} declares no "
            f"{_POLICY_KEY!r} block; the probe cadence is contract data and "
            "this node carries no code default to fall back on"
        )
    return ModelAlertChannelLivenessPolicy.model_validate(block)


__all__ = [
    "AlertChannelLivenessPolicyError",
    "ModelAlertChannelLivenessPolicy",
    "load_liveness_policy",
]
