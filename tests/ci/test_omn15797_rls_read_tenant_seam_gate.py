# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15797 AC3 -- the RLS tenant-seam gate's own RED/GREEN proof.

A gate whose only evidence is "it printed clean" proves nothing: a gate that
never fires and a gate that cannot fire look identical from the outside. These
tests pin both directions -- that the live tree is clean, AND that
re-introducing the exact defect shape makes the gate fail.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import omnimarket
from scripts.ci.check_rls_read_tenant_seam import main, rls_relations, scan

_SRC = Path(inspect.getfile(omnimarket)).parent


def test_selftest_passes() -> None:
    """The gate's built-in four-case RED/GREEN proof (also run in pre-commit)."""
    assert main(["--selftest"]) == 0


def test_live_tree_is_clean() -> None:
    """No unseamed RLS statement surface exists in src/omnimarket."""
    findings = scan(_SRC)
    assert findings == [], "\n".join(finding.render(_SRC) for finding in findings)


def test_rls_relations_are_derived_from_this_repos_migrations() -> None:
    """The relation set is derived, not hand-listed, so a new RLS migration
    widens the gate on the commit that adds the policy."""
    relations = rls_relations(_SRC)
    # Spot-check the relations named in OMN-15797's own live probe evidence.
    assert {"savings_estimates", "node_service_registry", "delegation_events"} <= (
        relations
    )


def test_reintroducing_the_defect_is_caught(tmp_path: Path) -> None:
    """The OMN-16092 shape verbatim: a driver import, a statement, an
    RLS-covered relation, and no tenant seam."""
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001.sql").write_text(
        "ALTER TABLE context_roi_scores ENABLE ROW LEVEL SECURITY;\n"
    )
    (tmp_path / "reader.py").write_text(
        "import psycopg2\n\n\n"
        "def read(conn):\n"
        "    with conn.cursor() as cur:\n"
        '        cur.execute("SELECT * FROM context_roi_scores")\n'
        "        return cur.fetchall()\n"
    )

    findings = scan(tmp_path)

    assert len(findings) == 1
    assert findings[0].relations == ("context_roi_scores",)
    assert "set_config" in findings[0].render(tmp_path)


def test_an_empty_relation_set_fails_closed(tmp_path: Path) -> None:
    """A scan that found no RLS migrations must refuse, not report clean --
    otherwise a path typo silently turns the gate into a no-op."""
    (tmp_path / "reader.py").write_text("import asyncpg\n")

    with pytest.raises(SystemExit) as excinfo:
        scan(tmp_path)

    assert excinfo.value.code == 2
