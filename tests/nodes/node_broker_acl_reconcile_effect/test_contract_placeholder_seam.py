# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A node contract's ${ONEX_ACL_*} placeholder names equal the declared list.

Seam: node contracts (src/omnimarket/nodes/*/contract.yaml, the ${NAME} names in
broker_acl_grants and broker_topics) -> node_broker_acl_reconcile_effect
(render_declaration fills every ${NAME} from the environment, the roles and the
placeholder_values of a principals overlay). A contract that adds, renames or drops a
${ONEX_ACL_*} placeholder without this list (and the overlay that fills it) being
updated fails here, naming the difference. The failing controls prove the scan
sees a new name and a dropped name.
Exemplar followed: test_role_placeholders.py (typed helpers, tmp_path contracts,
failing controls).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DECLARED_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "ONEX_ACL_BRANCH_DEPLOY_PRINCIPAL",
        "ONEX_ACL_BUS_MODEL_SEAM_PRINCIPAL",
        "ONEX_ACL_CODE_EDIT_PRINCIPAL",
        "ONEX_ACL_CONSUMER_GROUP_REAP_PRINCIPAL",
        "ONEX_ACL_CONSUMER_GROUP_STATE_PRINCIPAL",
        "ONEX_ACL_DEPLOY_AGENT_KEY_CHECK_PRINCIPAL",
        "ONEX_ACL_DESIRED_PUBLISHER_PRINCIPAL",
        "ONEX_ACL_DEV_ROLL_CAPTURE_PRINCIPAL",
        "ONEX_ACL_DEV_RUNTIME_EFFECTS_PRINCIPAL",
        "ONEX_ACL_FLEET_PACKAGE",
        "ONEX_ACL_GUARD_PRINCIPAL",
        "ONEX_ACL_HOST_RECONCILER_PRINCIPAL",
        "ONEX_ACL_LANE_READ_GUARD_PRINCIPAL",
        "ONEX_ACL_LAYER_REQUEST_PRINCIPAL",
        "ONEX_ACL_RELEASE_SIBLING_PREMISE_PRINCIPAL",
        "ONEX_ACL_RUNTIME_PRINCIPAL",
        "ONEX_ACL_STABILITY_REFRESH_SERVE_PRINCIPAL",
    }
)

PLACEHOLDER_PATTERN = re.compile(r"\$\{(ONEX_ACL_[A-Z0-9_]+)\}")


def contract_placeholders(root: Path) -> set[str]:
    """${ONEX_ACL_*} names in every ``*/contract.yaml`` file under ``root``."""
    found: set[str] = set()
    for path in sorted(root.glob("*/contract.yaml")):
        found.update(PLACEHOLDER_PATTERN.findall(path.read_text(encoding="utf-8")))
    return found


def test_contract_placeholders_equal_the_declared_list() -> None:
    """The checked-out contracts' ${ONEX_ACL_*} names are exactly the declared list."""
    root = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
    found = contract_placeholders(root)
    assert found == DECLARED_PLACEHOLDERS, (
        f"only in the contracts: {sorted(found - DECLARED_PLACEHOLDERS)}; "
        f"only in the declared list: {sorted(DECLARED_PLACEHOLDERS - found)}"
    )


def test_failing_control_new_placeholder_is_seen(tmp_path: Path) -> None:
    """Failing control: a contract adding ${ONEX_ACL_NEW_THING_PRINCIPAL} is seen by name."""
    contract = tmp_path / "node_x" / "contract.yaml"
    contract.parent.mkdir()
    contract.write_text(
        "broker_acl_grants:\n"
        "  - principal: ${ONEX_ACL_NEW_THING_PRINCIPAL}\n"
        "  - principal: ${ONEX_ACL_RUNTIME_PRINCIPAL}\n",
        encoding="utf-8",
    )
    found = contract_placeholders(tmp_path)
    assert found == {"ONEX_ACL_NEW_THING_PRINCIPAL", "ONEX_ACL_RUNTIME_PRINCIPAL"}
    assert found != DECLARED_PLACEHOLDERS


def test_failing_control_dropped_placeholder_is_seen(tmp_path: Path) -> None:
    """Failing control: a lone ${ONEX_ACL_RUNTIME_PRINCIPAL} is a strict subset."""
    contract = tmp_path / "node_y" / "contract.yaml"
    contract.parent.mkdir()
    contract.write_text(
        "broker_acl_grants:\n  - principal: ${ONEX_ACL_RUNTIME_PRINCIPAL}\n",
        encoding="utf-8",
    )
    found = contract_placeholders(tmp_path)
    assert found < DECLARED_PLACEHOLDERS
    assert found != DECLARED_PLACEHOLDERS
