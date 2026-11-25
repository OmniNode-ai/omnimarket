# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation check regression alarm's overlay document is a shared model the overlay home can pin (OMN-24562).

The overlay home pins a node's overlay to a merged revision that already holds the model, so the model lives
in the shared overlay documents package and merges ahead of the node that reads it.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_delegation_check_regression_overlay import (
    ModelRegressionSettings,
)

_DOC: dict[str, Any] = {
    "alarm_lane": "lane-a",
    "owner": "owner-a",
    "max_alarm_latency_seconds": 900,
    "scan_interval_seconds": 300,
    "flap_window_seconds": 86400,
    "flap_alarm_count": 2,
    "flap_grace_seconds": 600,
    "max_transition_age_seconds": 86400,
    "runs_per_check": 40,
    "gateway_timeout_seconds": 60,
    "bus_lane": "dev",
    "registry_root": "/registry/root",
    "dev_runtime_lane": "dev",
    "ledger_read": {
        "route": "file",
        "path_env": "LEDGER_FILE_VAR",
        "archive_subdir": "archive",
        "timeout_seconds": 60,
        "row_limit": 5000,
    },
    "checks": [
        {
            "check_id": "C11",
            "repository": "org/repo",
            "workflow": "c11.yml",
            "interval_seconds": 3600,
        },
        {
            "check_id": "C13",
            "repository": "org/repo",
            "workflow": "c13.yml",
            "interval_seconds": 3600,
        },
    ],
    "change_markers": [
        {"kind": "host_change", "row_type": "RULING", "contains_all": ["term"]},
    ],
    "merge_sources": [{"repository": "org/repo", "clone": "repo", "branch": "dev"}],
}


def test_the_complete_document_loads() -> None:
    doc = ModelRegressionSettings.model_validate(_DOC)

    assert [c.check_id for c in doc.checks] == ["C11", "C13"]
    assert doc.scan_interval_seconds == 300
    assert doc.ledger_read.route == "file"


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRegressionSettings.model_validate({**_DOC, "gateway_url": "x"})


@pytest.mark.parametrize("key", ["alarm_lane", "owner", "checks", "ledger_read"])
def test_a_document_missing_a_required_key_is_refused(key: str) -> None:
    broken = {k: v for k, v in _DOC.items() if k != key}

    with pytest.raises(ValidationError):
        ModelRegressionSettings.model_validate(broken)


def test_a_scan_interval_beyond_the_alarm_latency_is_refused() -> None:
    with pytest.raises(ValidationError, match="scan interval"):
        ModelRegressionSettings.model_validate({**_DOC, "scan_interval_seconds": 901})


def test_a_check_watched_twice_is_refused() -> None:
    doc = copy.deepcopy(_DOC)
    doc["checks"].append(dict(doc["checks"][0]))

    with pytest.raises(ValidationError, match="watched once"):
        ModelRegressionSettings.model_validate(doc)


def test_a_ledger_marker_of_the_merge_kind_is_refused() -> None:
    doc = copy.deepcopy(_DOC)
    doc["change_markers"][0]["kind"] = "merge"

    with pytest.raises(ValidationError):
        ModelRegressionSettings.model_validate(doc)
