# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157: the contract binds the ticket's catalogue criteria AC4 and AC6.

Each criterion names its falsifier in its statement. The evidence item that
binds it must run that falsifier unchanged, against a test file that exists,
in the default (hosted) scope, so the hosted dod-verify gate runs it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "contracts" / "OMN-20157.yaml"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _criterion(contract: dict[str, Any], label: str) -> dict[str, Any]:
    matches = [
        criterion
        for requirement in contract["requirements"]
        if requirement["id"] == "req-byok-catalogue-ticket-criteria"
        for criterion in requirement["acceptance"]
        if criterion["id"] == label
    ]
    assert len(matches) == 1, f"{label} is declared {len(matches)} times"
    return matches[0]


@pytest.mark.parametrize("label", ["AC4", "AC6"])
def test_the_binding_item_runs_the_falsifier_the_criterion_names(label: str) -> None:
    contract = _contract()
    statement = _criterion(contract, label)["statement"]
    falsifier = statement.rsplit("-- falsifier: ", 1)[1].strip()

    items = [item for item in contract["dod_evidence"] if label in item["binds_ac"]]
    assert len(items) == 1, f"{label} is bound by {len(items)} items"
    item = items[0]
    assert item.get("execution_scope") is None, "a hosted gate must run the binding"

    commands = [
        check["check_value"]
        for check in item["checks"]
        if check["check_type"] == "test_passes"
    ]
    assert commands == [falsifier]
    test_file = falsifier.split("pytest ", 1)[1].split()[0]
    assert (REPO_ROOT / test_file).is_file()
