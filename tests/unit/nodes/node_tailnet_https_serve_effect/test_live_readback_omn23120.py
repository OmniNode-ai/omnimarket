# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed live readback of the estimator's https front holds the facts its contract binds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

RECORD = (
    Path(__file__).resolve().parents[4]
    / "contracts/evidence/OMN-23120/live-readback.json"
)
FRONT = "https://<dev-host-tailnet-name> (tailnet only)"


@pytest.fixture(scope="module")
def record() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RECORD.read_text())
    return loaded


@pytest.mark.unit
def test_browser_run_reached_the_consent_step(record: dict[str, Any]) -> None:
    ac1 = record["ac1"]
    assert record["ticket_id"] == "OMN-23120"
    assert ac1["exit_code"] == 0
    load, _gate, consent = ac1["steps"]
    assert load["http_status"] == 200
    assert load["title"] == "Private preview | OmniNode"
    assert consent["consent_checkbox_count"] == 1
    assert consent["send_button_disabled_until_consent"] is True


@pytest.mark.unit
def test_uninstall_takes_the_front_away_and_reinstall_restores_it(
    record: dict[str, Any],
) -> None:
    ac3 = record["ac3"]
    before, down, up = ac3["before"], ac3["after_uninstall"], ac3["after_reinstall"]
    assert before["peer_curl_http"] == 200
    assert before["peer_curl_exit"] == 0
    assert FRONT in before["serve_status"]
    assert down["peer_curl_exit"] != 0
    assert FRONT not in down["serve_status"]
    assert up["peer_curl_http"] == 200
    assert up["peer_curl_exit"] == 0
    assert up["serve_status"] == before["serve_status"]
