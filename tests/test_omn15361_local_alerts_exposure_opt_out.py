# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local alerts exposure declares a reasoned opt-out until a reader exists (OMN-15361).

The exposure-reader-coverage gate in omnibase_infra refuses a served
``bus_backed`` exposure with no omnidash reader, no typed backend reader and no
reasoned opt-out. node_projection_local_alerts declares the exposure and no
reader exists yet, so the contract carries the self-expiring opt-out: both keys,
together. Failure modes: the opt-out loses its reason, loses its marker, or the
exposure it covers is renamed away from the topic the gate names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_alerts/contract.yaml"
)
TOPIC = "onex.snapshot.projection.local-alerts.v1"


def _projection_api() -> dict[str, Any]:
    with open(CONTRACT) as handle:
        return dict(yaml.safe_load(handle)["projection_api"])


def test_the_served_exposure_is_the_topic_the_gate_names() -> None:
    api = _projection_api()
    assert api["topic"] == TOPIC
    assert api["bus_backed"] is True


def test_the_exposure_opts_out_with_a_stated_reason() -> None:
    api = _projection_api()
    assert api["consumers"] == "none"
    assert str(api["consumers_reason"]).strip()


def test_no_backend_reader_is_declared_beside_the_opt_out() -> None:
    """The gate fails an opt-out left standing beside a reader: one or the other."""
    assert "backend_readers" not in _projection_api()
