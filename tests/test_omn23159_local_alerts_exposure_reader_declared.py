# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local-alerts exposure declares who reads it (OMN-23159, OMN-23414).

omnibase_infra's required check exposure-reader-coverage reads every
``bus_backed: true`` projection exposure in this repo's node contracts at the
omnimarket sha it pins.

OMN-23159 declared the exposure's ``consumers: none`` opt-out as deliberately
self-expiring, and said so in the contract: the gate fails a standing opt-out
once a reader exists, so the change that adds the reader deletes both keys.

OMN-23414 is that change. The reader exists: omnidash
``src/pages/local/status.contracts.yaml``, at the omnidash sha omnibase_infra
pins and on omnidash dev, declares the ``status-alerts-local-alerts`` data
binding on ``projection_topic: onex.snapshot.projection.local-alerts.v1``.
Recorded failure, the validator run over omnimarket and omnidash at the shas in
omnibase_infra ``.github/sibling-pins.yaml``, reached from
``tests/ci/test_omn22552_reader_coverage_at_pins.py``:

    exposure-reader-coverage FAIL ... - onex.snapshot.projection.local-alerts.v1
      code:   stale_opt_out
      reason: `consumers: none` is declared, but local-page:status.page.yaml now
              read this topic. Delete the opt-out; a standing opt-out over a live
              reader is how an escape hatch turns into a permanent amnesty entry.

The validator lives in omnibase_infra and cannot be imported here, so this test
pins the two rules of the gate this exposure has now crossed in both directions,
as the validator states them: a live reader means no ``consumers``/
``consumers_reason`` may stand, and the two keys are only ever declared together.
It checks the contract text only; the gate's own run against the pinned reader
surface is the authority.
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


def test_local_alerts_exposure_declares_no_opt_out_over_its_live_reader() -> None:
    block = _projection_api(CONTRACT)
    assert block["topic"] == TOPIC
    assert block["bus_backed"] is True
    assert "consumers" not in block, (
        "the omnidash status page declares the status-alerts-local-alerts reader for "
        "this exposure, so a `consumers: none` opt-out is a stale_opt_out to the gate "
        "(exposure-reader-coverage): delete it rather than keep an amnesty entry"
    )
    assert "consumers_reason" not in block, (
        "`consumers_reason` is deleted with `consumers: none`; the two are declared "
        "together or not at all"
    )


def test_exposure_stays_bus_backed_in_the_gate_input() -> None:
    """Deleting the opt-out must not hide the exposure from the gate."""
    block = _projection_api(CONTRACT)
    assert block["topic"] == TOPIC
    assert block["bus_backed"] is True, (
        "the exposure must stay bus_backed so the gate reads it: setting it false, or "
        "deleting the exposure, would clear the gate without a reader"
    )
    assert block["table"] == "local_alerts"
    assert block["schema"] == "omnidash_analytics"


def test_delegation_daily_exposure_declares_a_reasoned_opt_out() -> None:
    """No reader exists for the daily aggregate (no_reader): declare the gate's opt-out."""
    path = NODES / "node_projection_delegation" / "contract.yaml"
    data = yaml.safe_load(path.read_text())
    blocks = [
        b
        for b in data["projection_api"]["exposures"]
        if b.get("topic") == "onex.snapshot.projection.delegation.daily.v1"
    ]
    assert len(blocks) == 1
    assert blocks[0].get("consumers") == "none"
    assert str(blocks[0].get("consumers_reason", "")).strip()


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
