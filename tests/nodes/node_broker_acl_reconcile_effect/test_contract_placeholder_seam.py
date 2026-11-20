# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A node contract's ${ONEX_ACL_*} placeholder names equal what the node demands.

Seam: node contracts' ${ONEX_ACL_*} names -> node_broker_acl_reconcile_effect. The expectation comes from the
node's own reader (demanded_placeholders), so there is no list to keep in step; the overlays repository holds the
other side (the fills the overlay supplies).
Exemplar followed: test_role_placeholders.py (typed helpers, tmp_path contracts, failing controls).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    aggregate_declared_grants,
    demanded_placeholders,
    render_declaration,
)

pytestmark = pytest.mark.unit

PLACEHOLDER_PATTERN = re.compile(r"\$\{(ONEX_ACL_[A-Z0-9_]+)\}")


def contract_placeholders(root: Path) -> set[str]:
    """${ONEX_ACL_*} names in every ``*/contract.yaml`` file under ``root``."""
    found: set[str] = set()
    for path in sorted(root.glob("*/contract.yaml")):
        found.update(PLACEHOLDER_PATTERN.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_scan_equals_what_the_node_demands() -> None:
    """The checked-out contracts' ${ONEX_ACL_*} names are exactly the ONEX_ACL_ names the node demands."""
    root = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
    found = contract_placeholders(root)
    demanded = {n for n in demanded_placeholders() if n.startswith("ONEX_ACL_")}
    assert found == demanded, (
        f"only in the contracts: {sorted(found - demanded)}; "
        f"only in the demanded names: {sorted(demanded - found)}"
    )


def test_every_demanded_name_is_refused_when_unfilled() -> None:
    """The aggregated rows carry the unexpanded ${NAME} in their principal and name cells, so rendering with no
    fills names every demanded name; dropping one name from a full fills mapping names that one."""
    rows = aggregate_declared_grants()
    with pytest.raises(ValueError, match=r"^unset: ") as refused:
        render_declaration({"rows": rows, "principals": []}, {})
    message = str(refused.value)
    assert message.startswith("unset: ")
    listed = message.removeprefix("unset: ").split(", ")
    assert listed == sorted(demanded_placeholders())
    full = dict.fromkeys(demanded_placeholders(), "User:x")
    for name in sorted(demanded_placeholders()):
        fills = dict(full)
        del fills[name]
        with pytest.raises(ValueError, match=f"unset: .*{name}") as one:
            render_declaration({"rows": rows, "principals": []}, fills)
        assert name in str(one.value)


def test_failing_control_new_placeholder_is_seen(tmp_path: Path) -> None:
    """Failing control: a contract adding ${ONEX_ACL_NEW_THING_PRINCIPAL} is seen by the scan and demanded."""
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
    assert "ONEX_ACL_NEW_THING_PRINCIPAL" in demanded_placeholders(tmp_path)


def test_failing_control_a_name_in_a_key_the_node_does_not_read(tmp_path: Path) -> None:
    """Failing control: a ${ONEX_ACL_*} name in a key the node does not read is seen by the scan but not demanded."""
    contract = tmp_path / "node_y" / "contract.yaml"
    contract.parent.mkdir()
    contract.write_text(
        "notes: ${ONEX_ACL_NEW_THING_PRINCIPAL}\n",
        encoding="utf-8",
    )
    found = contract_placeholders(tmp_path)
    assert "ONEX_ACL_NEW_THING_PRINCIPAL" in found
    assert "ONEX_ACL_NEW_THING_PRINCIPAL" not in demanded_placeholders(tmp_path)
