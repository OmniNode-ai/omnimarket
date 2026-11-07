# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the retirement carrier adds no check a retired item already runs.

``supersedes_ac_binding`` withdraws a claim; it never alters the target item, so
the target keeps running its own checks (``resolve_retirements`` in
``node_dod_verify`` says so in its docstring). A carrier that also copies the
target's checks makes ``repo-evidence / dod-verify`` run each of them twice: the
copy of 228 checks took the contract from 388 to 616 checks and the job from its
28 to 40 minute reading to the 60 minute limit, where it ended cancelled with no
verdict on omnimarket#5150.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-17427.yaml"


def _key(check: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(check.get("check_type", "")),
        str(check.get("check_value", "")),
        str(check.get("cwd", "")),
    )


def duplicated_carrier_checks(items: Sequence[Mapping[str, Any]]) -> list[str]:
    """Checks a carrier item repeats from an item it retires a binding of."""
    by_id = {str(item.get("id")): item for item in items}
    repeated: list[str] = []
    for carrier in items:
        entries = carrier.get("supersedes_ac_binding")
        if not isinstance(entries, list):
            continue
        own = {_key(c) for c in carrier.get("checks") or []}
        for entry in entries:
            target = by_id.get(str(entry.get("item")))
            if target is None:
                continue
            for check in target.get("checks") or []:
                if _key(check) in own:
                    repeated.append(
                        f"{carrier.get('id')} repeats {target.get('id')}: "
                        f"{check.get('check_value')}"
                    )
    return repeated


def test_detector_reads_a_copied_check_as_a_duplicate() -> None:
    """Named injected break: a carrier that copies its target's check is found."""
    check = {"check_type": "test_passes", "check_value": "uv run pytest x -q"}
    items = [
        {"id": "target", "checks": [check]},
        {
            "id": "carrier",
            "checks": [dict(check)],
            "supersedes_ac_binding": [{"item": "target", "label": "AC1"}],
        },
    ]
    assert duplicated_carrier_checks(items) == [
        "carrier repeats target: uv run pytest x -q"
    ]


def test_detector_reads_a_carrier_with_its_own_check_as_clean() -> None:
    items = [
        {
            "id": "target",
            "checks": [{"check_type": "test_passes", "check_value": "a"}],
        },
        {
            "id": "carrier",
            "checks": [{"check_type": "test_passes", "check_value": "b"}],
            "supersedes_ac_binding": [{"item": "target", "label": "AC1"}],
        },
    ]
    assert duplicated_carrier_checks(items) == []


def test_omn17427_carriers_repeat_no_retired_item_check() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    repeated = duplicated_carrier_checks(document["dod_evidence"])
    assert not repeated, (
        f"{len(repeated)} carrier checks repeat a retired item's check, so "
        f"dod-verify runs each twice: {repeated[:3]}"
    )
