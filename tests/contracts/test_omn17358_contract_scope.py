# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-17358 contract binds no criterion to a ``local_done_gate`` item.

The hosted repo-evidence verifier runs neither the head nor the merge-base control for
an item scoped ``local_done_gate``, and refuses a criterion bound to one ("a control
that did not run is not a pass"). A criterion is bound to a check the hosted runner
runs; the lab proof stays as an unbound local item.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-17358.yaml"


def _items() -> list[dict[str, Any]]:
    loaded: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = loaded["dod_evidence"]
    return items


def test_no_local_done_gate_item_binds_a_criterion() -> None:
    offenders = [
        i["id"]
        for i in _items()
        if i.get("execution_scope") == "local_done_gate" and i.get("binds_ac")
    ]
    assert offenders == []


def test_ac1_is_bound_to_an_item_the_hosted_runner_runs() -> None:
    bound = [
        i
        for i in _items()
        if "AC1" in i.get("binds_ac", [])
        and i.get("execution_scope") != "local_done_gate"
    ]
    assert bound, "AC1 needs an item the hosted verifier runs"
