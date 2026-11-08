# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local-alerts exposure declares who reads it (OMN-23159).

omnibase_infra's required check exposure-reader-coverage reads every
``bus_backed: true`` projection exposure in this repo's node contracts at the
omnimarket sha it pins. Recorded failure (validator run on a lab host against the
omnimarket shas pinned by two open omnibase_infra pull requests, the bot pin
advance and the local-alerts interim-entry retirement, with the omnidash pin
unchanged):

    exposure-reader-coverage FAIL ... These exposures have no reader and no
    reasoned opt-out: onex.snapshot.projection.local-alerts.v1, code no_reader

The exposure shipped with ``bus_backed: true`` and neither a reader nor a reasoned
``consumers: none``, so every omnibase_infra pull request that advanced its pin
past that change read red. The validator lives in omnibase_infra and cannot be
imported here, so this test pins the two rules of the gate this exposure broke,
as the validator states them: ``consumers: none`` carries a non-empty
``consumers_reason``, and a ``consumers_reason`` without ``consumers: none`` is a
leftover. It checks the contract text only; the gate's own run against the
pinned reader surface is the authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
CONTRACT = NODES / "node_projection_local_alerts" / "contract.yaml"
TOPIC = "onex.snapshot.projection.local-alerts.v1"


def _projection_api(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    block = data.get("projection_api")
    assert isinstance(block, dict), f"{path} declares no projection_api"
    return block


def test_local_alerts_exposure_is_bus_backed_and_names_a_reasoned_opt_out() -> None:
    block = _projection_api(CONTRACT)
    assert block["topic"] == TOPIC
    assert block["bus_backed"] is True
    assert block.get("consumers") == "none", (
        "a bus_backed exposure with no omnidash or typed backend reader must declare "
        "`consumers: none` (exposure-reader-coverage, no_reader)"
    )
    reason = block.get("consumers_reason")
    assert isinstance(reason, str), "`consumers: none` needs a `consumers_reason`"
    assert reason.strip(), "`consumers: none` needs a non-empty `consumers_reason`"


def test_consumers_reason_never_stands_without_consumers_none() -> None:
    offenders: list[str] = []
    for contract in sorted(NODES.glob("*/contract.yaml")):
        data = yaml.safe_load(contract.read_text())
        block = data.get("projection_api") if isinstance(data, dict) else None
        if not isinstance(block, dict):
            continue
        has_reason = "consumers_reason" in block
        is_none = block.get("consumers") == "none"
        if has_reason != is_none:
            offenders.append(contract.parent.name)
    assert not offenders, (
        f"consumers and consumers_reason must be declared together: {offenders}"
    )
