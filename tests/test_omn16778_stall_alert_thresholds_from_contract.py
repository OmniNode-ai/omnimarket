# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 — thresholds are contract data, and nothing in Python overrides them.

*A test changes the declared threshold and observes the firing decision change
with no code edit. Falsified by any threshold literal in a* ``.py`` *file.*

Both halves are asserted here: the behavioural half (the same history decides
differently under a different contract) and the mechanical half (no threshold
literal survives in the node's Python sources, so there is no code default that
could satisfy the first half by accident).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers import (
    decide_stall_alert,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    EnumStallAlertOutcome,
    ModelConsumerFlowStallAlertRequest,
    ModelFlowWindowObservation,
    StallAlertPolicyError,
    load_stall_alert_policy,
)

NODE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_consumer_flow_stall_alert_effect"
)
CONTRACT_PATH = NODE_ROOT / "contract.yaml"

_EPOCH = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(minutes=1)

#: Field names whose values are the firing thresholds. A bare int assigned to
#: any of these inside a ``.py`` file would be a code default, which is exactly
#: what AC2 forbids.
_THRESHOLD_FIELDS = frozenset(
    {
        "confirm_windows",
        "clear_windows",
        "renotify_after_seconds",
        "unknown_warn_windows",
        "deliver_warnings",
        "alerting_states",
        # OMN-16778 redesign: the node now reads its own window history, so the
        # depth of that read and the per-trigger key ceiling are thresholds
        # too. A Python default for either would be a firing behaviour nobody
        # declared, exactly as a confirm-window default would be.
        "history_windows",
        "max_keys_per_trigger",
    }
)


def _stalled_history(count: int) -> tuple[ModelFlowWindowObservation, ...]:
    return tuple(
        ModelFlowWindowObservation(
            window_start=_EPOCH + i * _WINDOW,
            window_end=_EPOCH + (i + 1) * _WINDOW,
            flow_state=EnumConsumerFlowState.STALLED,
            messages_in=100,
            messages_out=0,
        )
        for i in range(count)
    )


@pytest.mark.unit
def test_raising_the_declared_confirm_window_suppresses_the_same_history(
    tmp_path: Path,
) -> None:
    """The identical history fires under one contract and not under another.

    No code is edited between the two evaluations; only the declared threshold
    differs. That is the whole of AC2.
    """
    shipped = load_stall_alert_policy(CONTRACT_PATH)
    history = _stalled_history(shipped.confirm_windows)

    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["alert_policy"]["confirm_windows"] = shipped.confirm_windows + 1
    stricter_path = tmp_path / "contract.yaml"
    stricter_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    stricter = load_stall_alert_policy(stricter_path)

    def _decide(policy: object) -> EnumStallAlertOutcome:
        return decide_stall_alert(
            ModelConsumerFlowStallAlertRequest(
                consumer_group="node_delegation_routing_reducer",
                topic="onex.cmd.omnimarket.delegate.v1",
                correlation_id=uuid4(),
                windows=history,
                policy=policy,  # type: ignore[arg-type]
            )
        ).outcome

    assert _decide(shipped) is EnumStallAlertOutcome.FAIL_CONFIRMED_STALL
    assert _decide(stricter) is EnumStallAlertOutcome.PENDING_CONFIRMATION


@pytest.mark.unit
def test_a_contract_without_an_alert_policy_fails_closed(tmp_path: Path) -> None:
    """No block, no fallback.

    A node that silently substitutes its own thresholds when the contract is
    unreadable is a node whose alerting behaviour is not the behaviour anyone
    declared.
    """
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    raw.pop("alert_policy")
    broken = tmp_path / "contract.yaml"
    broken.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(StallAlertPolicyError, match="alert_policy"):
        load_stall_alert_policy(broken)


@pytest.mark.unit
def test_unknown_is_never_declared_an_alerting_state() -> None:
    """A missed heartbeat must not be declarable as evidence of a stall."""
    policy = load_stall_alert_policy(CONTRACT_PATH)
    assert EnumConsumerFlowState.UNKNOWN not in policy.alerting_states


@pytest.mark.unit
def test_clear_threshold_is_stricter_than_the_confirm_threshold() -> None:
    """Asymmetric hysteresis, asserted rather than assumed.

    Clearing at least as fast as confirming means one healthy window cancels a
    confirmed stall, which is the flap this damping exists to stop.
    """
    policy = load_stall_alert_policy(CONTRACT_PATH)
    assert policy.clear_windows > policy.confirm_windows


@pytest.mark.unit
def test_no_threshold_literal_lives_in_python() -> None:
    """The mechanical half of AC2, over every ``.py`` file in this node.

    Walks the AST rather than grepping so a threshold hidden in a default
    argument, a class attribute or a keyword call is caught the same way a
    module-level constant would be.
    """
    offenders: list[str] = []
    for path in sorted(NODE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
                value = node.value
            elif isinstance(node, ast.keyword) and node.arg is not None:
                targets = [node.arg]
                value = node.value
            if not targets or value is None:
                continue
            if not isinstance(value, ast.Constant):
                continue
            if not isinstance(value.value, (int, bool)):
                continue
            for target in targets:
                if target in _THRESHOLD_FIELDS:
                    offenders.append(
                        f"{path.relative_to(NODE_ROOT)}:{node.lineno} "
                        f"{target}={value.value!r}"
                    )
    assert not offenders, (
        "thresholds are contract data (OMN-16778 AC2); these Python literals "
        f"would be code defaults: {offenders}"
    )
