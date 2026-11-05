# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Budgets, caps and bounds are ruled policy values read on every call (OMN-22019)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessVerdict,
)
from omnimarket.models.liveness.model_automation_liveness_deadman import (
    ModelAutomationLivenessDeadmanRequest,
)
from omnimarket.models.policy_values import (
    ENV_CONTROLLER_POLICY_OVERLAY,
    load_controller_policy_overlay,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers.handler_automation_liveness_deadman import (
    HandlerAutomationLivenessDeadman,
)
from omnimarket.nodes.node_cause_claim_ownership_effect import handler as ownership

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
_SRC = Path(ownership.__file__).resolve().parents[2]
CITED_FILES = (
    _SRC / "nodes" / "node_cause_claim_ownership_effect" / "handler.py",
    _SRC / "models" / "liveness" / "model_automation_liveness_deadman.py",
)


def _overlay(**raise_: int) -> dict[str, Any]:
    """The shipped overlay as a mapping, with any named value raised."""
    data = load_controller_policy_overlay().model_dump(mode="json")
    for section in ("automation_liveness_deadman", "cause_claim_ownership"):
        for name, value in raise_.items():
            if name in data[section]:
                data[section][name]["value"] = value
    return data


def _write(tmp_path: Path, data: dict[str, Any], name: str = "overlay.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_shipped_overlay_keeps_the_values_it_replaced() -> None:
    overlay = load_controller_policy_overlay()
    deadman = overlay.automation_liveness_deadman
    assert deadman.primary_heartbeat_bound_seconds.value == 10 * 60
    assert deadman.delivery_budget_seconds.value == 120
    assert deadman.canary_injection_hour_utc.value == 5
    assert deadman.canary_receipt_budget_seconds.value == 15 * 60
    ownership_policy = overlay.cause_claim_ownership
    assert ownership_policy.decisions_tail_bytes.value == 8 << 20
    assert ownership_policy.acting_window_seconds.value == 30 * 60


# ------------------------------------------------------------------------- AC3


def test_value_without_its_ruling_is_refused_at_load(tmp_path: Path) -> None:
    data = _overlay()
    del data["automation_liveness_deadman"]["primary_heartbeat_bound_seconds"]["ruling"]
    with pytest.raises(ValidationError, match="ruling"):
        load_controller_policy_overlay(_write(tmp_path, data))


def test_bare_number_without_a_ruling_is_refused_at_load(tmp_path: Path) -> None:
    data = _overlay()
    data["cause_claim_ownership"]["acting_window_seconds"] = 1800
    with pytest.raises(ValidationError):
        load_controller_policy_overlay(_write(tmp_path, data))


@pytest.mark.parametrize("ruling", ["", "TBD", "omn-1", "2026-10-21 21:14"])
def test_malformed_ruling_is_refused_at_load(tmp_path: Path, ruling: str) -> None:
    data = _overlay()
    data["cause_claim_ownership"]["decisions_tail_bytes"]["ruling"] = ruling
    with pytest.raises(ValidationError):
        load_controller_policy_overlay(_write(tmp_path, data))


def test_ruling_row_timestamp_is_accepted(tmp_path: Path) -> None:
    data = _overlay()
    data["cause_claim_ownership"]["decisions_tail_bytes"]["ruling"] = (
        "2026-10-21T21:14:58Z"
    )
    loaded = load_controller_policy_overlay(_write(tmp_path, data))
    assert loaded.cause_claim_ownership.decisions_tail_bytes.ruling == (
        "2026-10-21T21:14:58Z"
    )


def test_every_shipped_value_carries_a_ruling() -> None:
    dumped = load_controller_policy_overlay().model_dump()
    values = [
        v
        for section in dumped.values()
        if isinstance(section, dict)
        for v in section.values()
    ]
    assert values
    assert all(v["ruling"] for v in values)


# ------------------------------------------------------------------------- AC1


def test_raising_the_heartbeat_bound_flips_the_verdict_on_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compute = HandlerAutomationLivenessDeadman()

    def silent() -> bool:
        result = compute.handle(
            ModelAutomationLivenessDeadmanRequest(
                now=NOW,
                primary_host="fixture-host",
                canary_host="fixture-host",
                primary_last_heartbeat_at=NOW - timedelta(minutes=20),
            )
        )
        return EnumAutomationLivenessVerdict.PRIMARY_SILENT in {
            a.verdict for a in result.alarms_raised
        }

    path = _write(tmp_path, _overlay(primary_heartbeat_bound_seconds=600))
    monkeypatch.setenv(ENV_CONTROLLER_POLICY_OVERLAY, str(path))
    assert silent()

    _write(tmp_path, _overlay(primary_heartbeat_bound_seconds=3600))
    assert not silent()


def test_raising_the_acting_window_flips_the_verdict_on_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    landing = tmp_path / "landing"
    landing.mkdir()
    rows = [
        {
            "subject": "OmniNode-ai/omnimarket#1",
            "kind": "dispatch_worker",
            "outcome": "spawned",
            "lease_id": 7,
            "tick": 1,
            "ts": (NOW - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ]

    def acting() -> bool:
        return ownership.judge(
            rows, {}, landing, "OmniNode-ai/omnimarket#1", NOW, None
        )[0]

    path = _write(tmp_path, _overlay(acting_window_seconds=1800))
    monkeypatch.setenv(ENV_CONTROLLER_POLICY_OVERLAY, str(path))
    assert not acting()

    _write(tmp_path, _overlay(acting_window_seconds=3600))
    assert acting()


def test_decisions_tail_cap_is_read_from_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    landing = tmp_path / "landing"
    landing.mkdir()
    line = '{"subject": "repo#1", "kind": "noop", "ts": "2026-11-04T11:00:00Z"}\n'
    (landing / "decisions.jsonl").write_text(line * 50, encoding="utf-8")

    path = _write(tmp_path, _overlay(decisions_tail_bytes=8 << 20))
    monkeypatch.setenv(ENV_CONTROLLER_POLICY_OVERLAY, str(path))
    assert len(ownership.read_decisions(landing)) == 50

    _write(tmp_path, _overlay(decisions_tail_bytes=len(line) * 3))
    assert len(ownership.read_decisions(landing)) == 2


# ------------------------------------------------------------------------- AC2

_POLICY_WORDS = ("BUDGET", "CAP", "WINDOW", "BOUND", "BYTES", "TAIL", "LIMIT")


@pytest.mark.parametrize("path", CITED_FILES, ids=lambda p: p.name)
def test_no_module_level_budget_cap_window_or_bound_remains(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    ]
    assert names, "positive control: the file has module-level assignments"
    offenders = [n for n in names if any(w in n.upper() for w in _POLICY_WORDS)]
    assert offenders == []
