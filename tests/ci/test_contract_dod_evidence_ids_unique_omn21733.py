# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21733: a repo ticket contract names each dod_evidence item once.

A dev merge into a branch that also added criteria to the same contract repeated
``dod-omn21733-ac23`` to ``dod-omn21733-ac25``, and the repo evidence gate went red.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

#: libyaml's loader reads the same documents about ten times faster than the pure-Python one. This
#: file reads every ticket contract, and it is selected for a data-only pull request (OMN-26270).
_CONTRACT_LOADER: type[yaml.SafeLoader] | type[yaml.CSafeLoader] = getattr(
    yaml, "CSafeLoader", yaml.SafeLoader
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_repo_contract_names_each_dod_evidence_item_once() -> None:
    contracts = sorted((REPO_ROOT / "contracts").glob("OMN-*.yaml"))
    assert contracts, "expected at least one repo-owned contracts/OMN-*.yaml"
    offenders: list[str] = []
    for path in contracts:
        contract: dict[str, Any] = yaml.load(
            path.read_text(encoding="utf-8"), Loader=_CONTRACT_LOADER
        )
        counts = Counter(item["id"] for item in contract.get("dod_evidence", []))
        offenders.extend(
            f"{path.name}:{item_id} appears {count} times"
            for item_id, count in sorted(counts.items())
            if count > 1
        )
    assert not offenders, "\n".join(offenders)
