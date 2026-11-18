# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23865: the relock names the new infra build and every work contract records its hash.

omnimarket moves its omnibase-infra dev merge tag from
dev-20261111211012-df3f14f5e6b9 to dev-20261112010628-aa87c2dd6abe
(omnibase_infra#5901, the runtime account's grants on the two host disk
tables; core stays at dev-20261111160725-6cbe67b216fe), relocks, and the 70
work contracts that pin the lock re-record its sha256. The lock must name the
new infra build and every work contract that pins the lock must record the
hash of the new lock; the lock assertion fails at the merge base, where the
lock still names the old build.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / "uv.lock"
INFRA_LOCK_URL = (
    "omnibase_infra.git?tag=dev-20261113041401-2b0c3eacdf4d"
    "#2b0c3eacdf4d49b07ed1e97ad22b8972e37da75a"
)
CONTRACTS_GLOB = "src/omnimarket/configs/work_contracts/contracts/*.yaml"
LOCK_PIN_MARKER = "path: uv.lock"
SHA256_PREFIX = "sha256: "
MIN_CONTRACT_FILES = 70


def test_the_lock_names_the_pinned_infra_build() -> None:
    """The relocked uv.lock names the dev merge tag and commit of the pinned infra build (moved to 2b0c3eacdf4d by OMN-24071)."""
    lock_text = LOCK_PATH.read_text(encoding="utf-8")
    assert INFRA_LOCK_URL in lock_text


def test_every_work_contract_records_the_hash_of_the_lock() -> None:
    """Every work contract that pins the lock records the hash of the relocked uv.lock."""
    digest = hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest()
    expected = SHA256_PREFIX + digest
    contracts = sorted(REPO_ROOT.glob(CONTRACTS_GLOB))
    assert len(contracts) >= MIN_CONTRACT_FILES
    offending: list[str] = []
    for contract in contracts:
        text = contract.read_text(encoding="utf-8")
        if LOCK_PIN_MARKER in text and expected not in text:
            offending.append(contract.name)
    assert offending == []
