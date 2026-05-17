"""OMN-11011 migration ownership regression checks."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(",
    re.IGNORECASE,
)
EXPECTED_PACKAGE_MIGRATIONS = {
    "nightly_loop_decisions": Path(
        "src/omnimarket/nodes/node_nightly_loop_controller/migrations/"
        "001_create_nightly_loop_tables.sql"
    ),
    "nightly_loop_iterations": Path(
        "src/omnimarket/nodes/node_nightly_loop_controller/migrations/"
        "001_create_nightly_loop_tables.sql"
    ),
    "review_bot_bypass_log": Path(
        "src/omnimarket/nodes/node_pr_review_bot/migrations/"
        "001_create_review_bot_bypass_log.sql"
    ),
}


def test_omn11011_package_migrations_have_single_owners() -> None:
    """The OMN-11011 package tables are intentionally owned by omnimarket nodes."""
    locations: dict[str, list[Path]] = {
        table: [] for table in EXPECTED_PACKAGE_MIGRATIONS
    }

    for migration in sorted(
        (REPO_ROOT / "src" / "omnimarket" / "nodes").glob("**/migrations/**/*.sql")
    ):
        content = migration.read_text(encoding="utf-8")
        for match in CREATE_TABLE_RE.finditer(content):
            table = match.group(1).lower()
            if table in locations:
                locations[table].append(migration.relative_to(REPO_ROOT))

    assert locations == {
        table: [expected] for table, expected in EXPECTED_PACKAGE_MIGRATIONS.items()
    }
