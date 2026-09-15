#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18043 — every ``projection_api`` exposure must declare a ``cursor_column``.

Without one, ``api_server.py`` cannot populate ``next_cursor``: the serving path
gates on ``cfg.cursor_column is not None``, so a truncated page silently reports
``next_cursor: null`` and a caller cannot distinguish "this is the whole set"
from "this is page 1 of N". Measured on the dev lane 2026-09-09: consumer-flow
serves ``row_count=500 row_limit=500 next_cursor=None``, and ``?limit=2`` over
the same rows also returns ``next_cursor=None`` — unambiguously truncated, no
cursor. OMN-17215's repair is merged and deployed and cannot fire, because that
exposure declares no cursor column.

**Scope, deliberately narrow.** This checks that a cursor column is DECLARED. It
does not check that the named column is unique or monotonic — that is the other
half of OMN-18043 AC2 and it is not decidable from the contract, which carries
no column types at all. Measured: ``projection_cursor`` is ``BIGSERIAL PRIMARY
KEY`` in ``pr_merged_events`` and ``TEXT`` in ``evidence_dashboard_projection``.
Same name, different semantics. Validating that needs the migration or the live
catalogue, and which of those is a separate decision.

**Ratchet, not a green-field assertion.** 56 of 62 exposures violate this today,
so a hard failure would block every commit in the repo. The baseline records the
known violations; this gate fails only when the set GROWS. Same shape as
``topic_naming_baseline.txt`` and ``state_coverage_baseline.txt`` here, and the
pattern OMN-16554's DoD prescribes for exactly this situation.

Regenerate after legitimately declaring a cursor somewhere::

    uv run python scripts/validation/check_projection_cursor_declared.py --write-baseline

The baseline may only shrink. A regeneration that adds entries is rejected
unless ``--allow-growth`` is passed, so "fix the baseline" cannot quietly become
the way a new violation lands.

**A second, HARD class: the declared cursor must be a declared column.** An
exposure that declares ``cursor_column`` naming a column absent from its own
``columns`` list fails unconditionally. The serving path reads the cursor value
off the selected row, and a row that never carries the column cannot yield a
``next_cursor``. No legacy population predates this, so it is never baselined:
an entry in the baseline does not excuse it, and ``--write-baseline`` refuses to
run while one exists (``--allow-growth`` included). Membership is compared the
way discovery compares declared columns — surrounding double quotes stripped on
both sides. ``columns: ["*"]`` (SELECT *) is accepted without a membership
check, matching discovery and ``ProjectionTableConfig``, which treat ``("*",)``
as carrying every column for ``order_by``, ``order_rank`` and ``tenant_column``;
the contract alone cannot decide membership there, and the migration is the
authority on what the table holds.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"
BASELINE = Path(__file__).resolve().parent / "projection_cursor_baseline.txt"


def _exposures(contract: dict[str, object]) -> list[dict[str, object]]:
    """Every exposed ``projection_api`` block, in BOTH declared shapes.

    The section is written two ways across this repo — flat (config directly on
    ``projection_api``) and nested (a list under ``exposures``). A reader that
    handles only the flat form sees the nested ones as declaring nothing, which
    undercounts by 20 exposures and reads their config as absent. That mistake
    is not hypothetical: it produced a wrong fleet count on 2026-09-08 and had
    to be withdrawn and re-measured.
    """
    section = contract.get("projection_api")
    if not isinstance(section, dict) or section.get("expose") is not True:
        return []
    nested = section.get("exposures")
    if isinstance(nested, list):
        return [e for e in nested if isinstance(e, dict)]
    return [section]


def _tracked_exposures() -> list[tuple[str, dict[str, object]]]:
    """``(exposure_id, exposure)`` for every exposure in the tree, in stable order."""
    tracked: list[tuple[str, dict[str, object]]] = []
    for contract_path in sorted(NODES_DIR.glob("node_*/contract.yaml")):
        try:
            contract = yaml.safe_load(contract_path.read_text())
        except (
            yaml.YAMLError
        ) as exc:  # a malformed contract is not this gate's business
            print(
                f"warning: skipping unparseable {contract_path}: {exc}", file=sys.stderr
            )
            continue
        if not isinstance(contract, dict):
            continue
        node = contract_path.parent.name
        for index, exposure in enumerate(_exposures(contract)):
            table = exposure.get("table") or "unnamed"
            tracked.append((f"{node}::{table}#{index}", exposure))
    return tracked


def violations(
    tracked: list[tuple[str, dict[str, object]]] | None = None,
) -> list[str]:
    """Exposure ids that declare no ``cursor_column``, sorted and stable.

    The id carries the exposure INDEX, not just node and table. Several nodes
    expose the same table more than once — ``node_projection_delegation``
    exposes ``delegation_events`` four times — so a ``node::table`` key collapses
    5 of the 56 current violations into shared entries. That is not cosmetic: a
    NEW violating exposure added to an already-baselined table would produce a
    key the baseline already contains and land silently, which is the exact
    failure this gate exists to prevent.
    """
    if tracked is None:
        tracked = _tracked_exposures()
    return sorted(
        exposure_id
        for exposure_id, exposure in tracked
        if not exposure.get("cursor_column")
    )


def _cursor_membership_problem(exposure: dict[str, object]) -> str | None:
    """Why a DECLARED ``cursor_column`` is not a declared column, else ``None``.

    An exposure with no cursor is the ratchet's business, not this check's.
    """
    cursor = exposure.get("cursor_column")
    if not cursor:
        return None
    columns = exposure.get("columns")
    if not isinstance(columns, list):
        return (
            f"cursor_column {cursor!r} is declared but the exposure declares no "
            "columns list"
        )
    if columns == ["*"]:
        return None  # SELECT *: membership is not decidable from the contract
    declared = {c.strip('"') for c in columns if isinstance(c, str)}
    if not isinstance(cursor, str) or cursor.strip('"') not in declared:
        return (
            f"cursor_column {cursor!r} is not among the declared columns "
            f"{columns!r} (missing column: {cursor!r})"
        )
    return None


def membership_violations(
    tracked: list[tuple[str, dict[str, object]]] | None = None,
) -> list[tuple[str, str]]:
    """``(exposure_id, reason)`` for each declared cursor absent from ``columns``.

    HARD: never baselined. Keyed with the same ``node::table#index`` id as
    :func:`violations` so the output names the exact exposure.
    """
    if tracked is None:
        tracked = _tracked_exposures()
    found: list[tuple[str, str]] = []
    for exposure_id, exposure in tracked:
        problem = _cursor_membership_problem(exposure)
        if problem is not None:
            found.append((exposure_id, problem))
    return sorted(found)


def _read_baseline() -> list[str]:
    if not BASELINE.exists():
        return []
    return sorted(
        line.strip()
        for line in BASELINE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="regenerate the baseline from the current tree",
    )
    parser.add_argument(
        "--allow-growth",
        action="store_true",
        help="permit --write-baseline to record MORE violations than before",
    )
    args = parser.parse_args()

    tracked = _tracked_exposures()
    current = violations(tracked)
    hard = membership_violations(tracked)
    baseline = _read_baseline()

    if hard:
        print(
            f"FAIL: {len(hard)} projection_api exposure(s) declare a cursor_column that "
            "is not among their declared columns:\n"
            + "".join(f"  {exposure_id}: {reason}\n" for exposure_id, reason in hard)
            + "\nThis is never baselined. Add the column to `columns`, or declare the "
            "cursor_column the exposure actually selects.",
            file=sys.stderr,
        )
        if args.write_baseline:
            print(
                "REFUSING to write a baseline while a cursor_column is absent from its "
                "declared columns (--allow-growth does not apply).",
                file=sys.stderr,
            )
            return 1

    if args.write_baseline:
        added = sorted(set(current) - set(baseline))
        # First write establishes the ratchet; it is not growth. Every later
        # write is compared against what is already recorded.
        bootstrapping = not BASELINE.exists()
        if added and not args.allow_growth and not bootstrapping:
            print(
                "REFUSING to write a baseline that grows.\n"
                "These exposures are NEW violations, not pre-existing ones:\n"
                + "".join(f"  + {item}\n" for item in added)
                + "Declare a cursor_column instead, or pass --allow-growth with a reason.",
                file=sys.stderr,
            )
            return 1
        header = (
            "# OMN-18043 — projection_api exposures that declare no cursor_column.\n"
            "# This list may only SHRINK. Regenerate with:\n"
            "#   uv run python scripts/validation/check_projection_cursor_declared.py --write-baseline\n"
        )
        BASELINE.write_text(header + "".join(f"{item}\n" for item in current))
        print(f"baseline written: {len(current)} exposures")
        return 0

    new = sorted(set(current) - set(baseline))
    fixed = sorted(set(baseline) - set(current))

    if new:
        print(
            f"FAIL: {len(new)} projection_api exposure(s) declare no cursor_column and are "
            "not in the baseline:\n"
            + "".join(f"  {item}\n" for item in new)
            + "\nWithout a cursor_column the serving path cannot populate next_cursor "
            "(api_server.py gates on `cfg.cursor_column is not None`), so a truncated "
            "page reports next_cursor: null and a caller cannot tell it is truncated.",
            file=sys.stderr,
        )
        return 1

    if hard:
        return 1

    if fixed:
        print(
            f"{len(fixed)} exposure(s) now declare a cursor_column and can leave the "
            "baseline — run --write-baseline to shrink it:\n"
            + "".join(f"  {item}\n" for item in fixed)
        )

    print(f"OK: {len(current)} exposure(s) without a cursor_column, all baselined.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
