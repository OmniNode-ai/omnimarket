# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The firing policy, read from ``contract.yaml`` and never from code.

OMN-16778 AC2 is explicit: *a test changes the declared threshold and observes
the firing decision change with no code edit; falsified by any threshold
literal in a ``.py`` file.*

So this model declares **no defaults**.  Every field is required, the loader
fails closed on a missing or malformed ``alert_policy`` block, and
``tests/test_omn16778_stall_alert_thresholds_from_contract.py`` asserts
mechanically that no bare integer threshold appears anywhere under this node's
Python sources.  A default here would be a threshold nobody can change without
a deploy, which is the thing the AC forbids.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState

_POLICY_KEY = "alert_policy"


class ModelStallAlertPolicy(BaseModel):
    """Contract-declared thresholds for one stall-alert evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confirm_windows: int = Field(
        ...,
        ge=1,
        description="Consecutive alerting windows required before a FAIL fires.",
    )
    clear_windows: int = Field(
        ...,
        ge=1,
        description=(
            "Consecutive non-alerting windows required before a confirmed "
            "stall is considered recovered."
        ),
    )
    renotify_after_seconds: int = Field(
        ...,
        ge=1,
        description=(
            "A still-firing condition re-posts at most once per this many "
            "seconds. Enforced by bucketing the idempotency key, so the "
            "existing node_slack_publish_effect ledger does the deduplication "
            "and this node keeps no state of its own."
        ),
    )
    alerting_states: tuple[EnumConsumerFlowState, ...] = Field(
        ...,
        min_length=1,
        description="Flow states that count as a stall for alerting purposes.",
    )
    require_failure_evidence_for: tuple[EnumConsumerFlowState, ...] = Field(
        ...,
        description=(
            "Alerting states that additionally require independent failure "
            "evidence -- a dead-letter or a handler error in the same window -- "
            "before the window counts toward an alerting run. Declared, not "
            "assumed: see the class docstring for why an out-count of zero is "
            "not on its own evidence of a dead leg."
        ),
    )
    unknown_warn_windows: int = Field(
        ...,
        ge=1,
        description=(
            "Consecutive UNKNOWN windows before the missed-heartbeat WARN is surfaced."
        ),
    )
    deliver_warnings: bool = Field(
        ...,
        description=(
            "Whether a WARN is pushed to Slack as well as surfaced in the "
            "terminal event. FAIL and WARN are different facts; delivering "
            "both by default is how a channel gets muted."
        ),
    )

    def is_alerting(self, state: EnumConsumerFlowState) -> bool:
        """Whether ``state`` counts as a stall under this policy."""
        return state in self.alerting_states

    def needs_failure_evidence(self, state: EnumConsumerFlowState) -> bool:
        """Whether ``state`` may only alert alongside a DLQ or handler error.

        ``STALLED`` is declared this way because an out-count of zero is not,
        by itself, evidence of a dead leg. Two whole classes of healthy node
        publish nothing at all: a projection writes rows to Postgres, and a
        compute delivers its intent IN-PROCESS through
        ``IntentEffectDispatchBridge`` -- which is why
        ``node_gateway_link_health_projection_compute`` sits at Kafka
        high-watermark 0 while fully alive (falsified premise, 2026-08-29).

        The platform carries no counter that separates those from a consumer
        that has genuinely died, so the alert asks for corroboration instead.
        Measured on the ``.201`` dev lane at 2026-08-29T02:40Z the split is
        total: every real failure showed ``messages_dlq == messages_in`` and
        ``handler_errors == messages_in`` (this very node's 8-error validation
        bug, ``pattern_b_broker``, ``node_pr_lifecycle_state_reducer``), while
        every non-publishing-but-healthy leg showed ``dlq=0 errors=0``
        (``projection_consumer_flow``, ``projection_registration``,
        ``node_registration_orchestrator``, ``projection_live_events``).
        Without this rule the node's first live dispatch would have posted nine
        alerts, most of them wrong -- the AC3 alert storm that gets a channel
        muted inside a day.

        ``STARVED`` is deliberately NOT declared this way: in == 0 while
        upstream is producing means the consumer is not taking messages at all,
        which needs no second witness.
        """
        return state in self.require_failure_evidence_for


class StallAlertPolicyError(RuntimeError):
    """The contract does not declare a usable ``alert_policy``.

    Raised rather than defaulted. A node that silently substitutes its own
    thresholds when the contract is unreadable is a node whose alerting
    behaviour is not the behaviour anyone declared.
    """


def load_stall_alert_policy(contract_path: Path) -> ModelStallAlertPolicy:
    """Read the ``alert_policy`` block out of a node contract.

    Args:
        contract_path: Path to the ``contract.yaml`` to read. Passed in rather
            than resolved from a module-level constant so a test can point it
            at a modified copy and watch the decision change -- which is
            exactly what AC2 requires be possible without a code edit.

    Returns:
        The parsed, fully-required policy.

    Raises:
        StallAlertPolicyError: The file is missing, unparseable, carries no
            ``alert_policy`` block, or declares a block that does not validate.
    """
    try:
        raw: Any = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StallAlertPolicyError(
            f"cannot read stall-alert contract at {contract_path}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise StallAlertPolicyError(
            f"stall-alert contract at {contract_path} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise StallAlertPolicyError(
            f"stall-alert contract at {contract_path} is not a mapping"
        )
    block = raw.get(_POLICY_KEY)
    if not isinstance(block, dict):
        raise StallAlertPolicyError(
            f"stall-alert contract at {contract_path} declares no "
            f"{_POLICY_KEY!r} block; thresholds are contract data and this "
            "node carries no code defaults to fall back on"
        )
    return ModelStallAlertPolicy.model_validate(block)


__all__ = [
    "ModelStallAlertPolicy",
    "StallAlertPolicyError",
    "load_stall_alert_policy",
]
