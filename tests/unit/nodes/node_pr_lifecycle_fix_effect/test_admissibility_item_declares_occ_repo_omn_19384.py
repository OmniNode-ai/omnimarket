# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19384: the minted admissibility item declares the repo it runs in.

``uv run pytest tests/test_evidence_admissibility.py -q`` runs a file that only
onex_change_control has. The item carried no ``cwd``, so a product repo's
Contract Compliance ran it in the product checkout, where it exits 4 or 5 and
BLOCKs (omnibase_core#1818, the 0.47.27 release). The item now carries
``cwd: ${OMNI_HOME}/onex_change_control``, the same declaration the
behaviour-proof item already makes for its product repo.
"""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ADMISSIBILITY_VALIDATOR_CWD,
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    render_admissibility_validator_dod_evidence_item,
    render_slot_dod_evidence_item,
)

pytestmark = pytest.mark.unit


def _parse(block: str) -> dict[str, object]:
    items = yaml.safe_load("dod_evidence:\n" + block)["dod_evidence"]
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, dict)
    return item


@pytest.mark.parametrize("superseded", [None, "dod-OmniNode-ai-omnibase_core-pr-1"])
def test_the_admissibility_check_declares_the_change_control_tree(
    superseded: str | None,
) -> None:
    item = _parse(
        render_admissibility_validator_dod_evidence_item(
            superseded_evidence_id=superseded,
            evidence_id=f"{ADMISSIBILITY_VALIDATOR_EVIDENCE_ID}-pr-1818",
        )
    )
    assert item["checks"] == [
        {
            "check_type": "command",
            "check_value": ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
            "cwd": "${OMNI_HOME}/onex_change_control",
        }
    ]
    assert ADMISSIBILITY_VALIDATOR_CWD == "${OMNI_HOME}/onex_change_control"


def test_a_docs_only_product_pr_slot_item_declares_the_change_control_tree() -> None:
    """The omnibase_core#1818 shape: a CHANGELOG-only release PR, no test to derive."""
    item = _parse(
        render_slot_dod_evidence_item(
            repo="OmniNode-ai/omnibase_core",
            pr_number=1818,
            changed_files=["CHANGELOG.md"],
            superseded_evidence_id=None,
        )
    )
    assert item["id"] == f"{ADMISSIBILITY_VALIDATOR_EVIDENCE_ID}-pr-1818"
    checks = item["checks"]
    assert isinstance(checks, list)
    assert [check["cwd"] for check in checks] == ["${OMNI_HOME}/onex_change_control"]


def test_the_rendered_item_validates_against_the_contract_schema() -> None:
    from omnibase_core.models.ticket.model_contract_dod_item import (
        ModelContractDodItem,
    )

    item = _parse(
        render_admissibility_validator_dod_evidence_item(
            evidence_id=f"{ADMISSIBILITY_VALIDATOR_EVIDENCE_ID}-pr-1818"
        )
    )
    validated = ModelContractDodItem.model_validate(item)
    assert validated.checks[0].cwd == "${OMNI_HOME}/onex_change_control"
