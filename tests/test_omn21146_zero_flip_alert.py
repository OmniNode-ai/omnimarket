# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21146: completion alerts on contract-driven zero-flip streaks with deferrals."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_dod_closeout_sweep_compute as node_package
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumCloserHealth,
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).resolve().parent
CONTRACT_PATH = NODE_DIR / "contract.yaml"
HANDLER = HandlerDodCloseoutSweep()
DATE = "2026-10-06"
STAMP = "2026-10-06T03:00:00Z"
TOPIC = "onex.evt.omnimarket.dod-closeout-sweep-decided.v1"


def receipt(run: int, flipped: int = 0) -> str:
    return (
        f"2026-10-06T{run:02d}:00:00Z | TERMINAL | lane=dod-closeout-sweep | "
        f"run=run-{run} | flipped={flipped} | friction=none"
    )


def summary(**overrides: Any) -> dict[str, Any]:
    run: dict[str, Any] = {
        "run_key": "run-3",
        "selected": ["OMN-1", "OMN-2"],
        "flipped": [],
        "deferred_count": 57,
        "held_for_amendment": [],
        "open": [{"id": "OMN-1", "unmet": [{"reason": "binding refused (no-cwd)"}]}],
        "refused_bindings": [{"refused": "no-cwd"}, {"refused": "no-cwd"}],
    }
    run.update(overrides)
    return run


def complete(
    rows: list[str], run: dict[str, Any] | None = None, **fields: Any
) -> ModelDodCloseoutDecisionResult:
    return HANDLER.handle(
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "completion",
                "date": DATE,
                "clock_utc": STAMP,
                "ledger_rows": rows,
                "run_summary": summary() if run is None else run,
                **fields,
            }
        )
    )


@pytest.mark.parametrize("length", [3, 4])
def test_completion_returns_alert_at_threshold_and_on_later_zero_runs(
    length: int,
) -> None:
    result = complete(
        [receipt(i) for i in range(1, length)], summary(run_key=f"run-{length}")
    )
    assert result.zero_flip_streak == length
    assert result.zero_flip_alert is not None
    assert result.zero_flip_alert.model_dump() == {
        "lane": "dod-closeout-sweep",
        "run_key": f"run-{length}",
        "zero_flip_streak": length,
        "threshold_runs": 3,
        "deferred_count": 57,
        "top_deferral_reason": "binding-refused:no-cwd",
        "observed_at": STAMP,
    }


def test_no_deferred_tickets_suppresses_alert_but_preserves_friction() -> None:
    result = complete([receipt(1), receipt(2)], summary(deferred_count=0))
    assert result.zero_flip_streak == 3
    assert result.zero_flip_alert is None
    assert result.completion_rows is not None
    assert [row.split(" | ")[1] for row in result.completion_rows] == [
        "FRICTION",
        "TERMINAL",
    ]
    assert f"friction={STAMP} lane=dod-closeout-sweep" in result.completion_rows[-1]


def test_one_run_below_threshold_has_no_alert() -> None:
    result = complete([receipt(1)])
    assert result.zero_flip_streak == 2
    assert result.zero_flip_alert is None
    assert result.completion_rows is not None
    assert [row.split(" | ")[1] for row in result.completion_rows] == ["TERMINAL"]


def test_a_flip_resets_the_streak_and_suppresses_alert() -> None:
    result = complete([receipt(1), receipt(2)], summary(flipped=["OMN-1"]))
    assert result.zero_flip_streak == 0
    assert result.zero_flip_alert is None
    assert result.completion_rows is not None
    assert [row.split(" | ")[1] for row in result.completion_rows] == ["TERMINAL"]


@pytest.mark.parametrize(
    ("threshold", "expected_rows", "expected_health"),
    [
        (5, ["TERMINAL"], EnumCloserHealth.GREEN),
        (3, ["FRICTION", "TERMINAL"], EnumCloserHealth.RED),
    ],
)
def test_threshold_input_controls_alert_friction_and_health(
    threshold: int, expected_rows: list[str], expected_health: EnumCloserHealth
) -> None:
    result = complete([receipt(1), receipt(2)], zero_flip_threshold_runs=threshold)
    assert result.zero_flip_streak == 3
    assert (result.zero_flip_alert is not None) == (threshold == 3)
    if result.zero_flip_alert is not None:
        assert result.zero_flip_alert.threshold_runs == threshold
    assert result.completion_rows is not None
    assert [row.split(" | ")[1] for row in result.completion_rows] == expected_rows
    health = HANDLER.handle(
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "ground_state",
                "date": DATE,
                "ledger_rows": [receipt(1), receipt(2), receipt(3)],
                "zero_flip_threshold_runs": threshold,
            }
        )
    )
    assert health.zero_flip_streak == 3
    assert health.health is expected_health
    assert health.ground_state_row is not None
    assert f"| {expected_health.value} |" in health.ground_state_row


def test_contract_model_default_parity_and_alert_rides_the_terminal_event() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert "zero_flip_alert" not in raw
    threshold_input = raw["inputs"]["zero_flip_threshold_runs"]
    assert threshold_input["type"] == "int"
    assert threshold_input["required"] is False
    assert (
        threshold_input["default"]
        == ModelDodCloseoutDecisionRequest.model_fields[
            "zero_flip_threshold_runs"
        ].default
    )
    # The alert rides on the terminal event the runtime publishes for the node.
    assert raw["terminal_event"] == TOPIC
    assert TOPIC in raw["event_bus"]["publish_topics"]
    assert TOPIC in raw["externally_consumed_topics"]
    assert "zero_flip_alert" in raw["outputs"]


@pytest.mark.parametrize("threshold", [0, -1, "invalid", None])
@pytest.mark.parametrize("kind", ["completion", "ground_state"])
def test_invalid_threshold_input_is_rejected(threshold: object, kind: str) -> None:
    request = {
        "kind": kind,
        "date": DATE,
        "ledger_rows": [],
        "zero_flip_threshold_runs": threshold,
    }
    if kind == "completion":
        request.update(clock_utc=STAMP, run_summary=summary())
    with pytest.raises(ValidationError, match="zero_flip_threshold_runs"):
        ModelDodCloseoutDecisionRequest.model_validate(request)


def test_no_threshold_constant_or_purity_suppression_in_node_python() -> None:
    offenders: list[str] = []
    for path in sorted(NODE_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "node-purity-ok" in source:
            offenders.append(f"{path.name}: node-purity-ok suppression")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "THRESHOLD"
                for target in targets
            ):
                offenders.append(f"{path.name}:{node.lineno} THRESHOLD assignment")
    assert not offenders, f"OMN-21146: thresholds are pure request inputs: {offenders}"
