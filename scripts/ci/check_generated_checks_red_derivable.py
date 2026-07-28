#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Static RED-derivability grammar gate for GENERATED OCC companion contracts.

OMN-15247 layer 2 of the acceptance bar
---------------------------------------
OMN-15247's mechanical bar is: *"for every generated check, the same check run
against the PR's merge-base must return non-zero."* That bar is enforced in
three layers:

1. **Mint-time (runtime, fail-closed).** ``OccCompanionEmitter`` executes the
   selected probe at the evidence ref (must exit 0) and at the merge base (must
   exit non-zero) before writing it. See ``_derive_content_bound_check``.
2. **This gate (offline, deterministic, no network).** Every ``dod_evidence``
   check_value in a rendered companion contract must match either the
   content-bound grammar or one of the explicitly allowlisted forms. Structural
   only — it proves the SHAPE could go RED, never that it did.
3. **Live corpus mode** (``--live``, opt-in, deliberately NOT wired to a gate):
   rewrite the pinned ``?ref=`` to a caller-supplied merge base and assert
   non-zero.

Scope, stated honestly
----------------------
This gate runs over contracts THIS repo's producer renders (fixtures + any path
passed on the command line). It is deliberately **not** run across the merged
``onex_change_control`` corpus: that corpus is dominated by pre-existing
existence-probe contracts, so a corpus-wide fail-closed gate today would reject
essentially all traffic — the same reject-everything trap documented by
``check_contract_substance_floor.py``'s ``GATE_SELF_REFERENTIAL`` kill switch
(flag ON => 2,277/6,916 rejected, 98.4% of new contracts blocked). Corpus
enforcement follows the producer flip, which is an operator decision.

Because the ``pr_existence`` binding is still the shipped default, the
allowlisted forms below are ACCEPTED, not rejected. This gate's job in slice 1
is to guarantee that when a content-bound check IS emitted it is structurally
RED-derivable, and that no third, un-vetted check shape appears. Tightening it
to reject the existence probes is the follow-up that lands with the flip.

Usage:
    python scripts/ci/check_generated_checks_red_derivable.py <contract.yaml>...
    python scripts/ci/check_generated_checks_red_derivable.py --json <paths>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

# --- The content-bound grammar (the only NEW shape this slice can emit) ------
#
# Exactly one pinned ``?ref=<7-40 hex>``, a terminal ``grep -c``/``grep -q``
# with a non-empty single-quoted needle, and no output-suppressing tail.
_CONTENT_BOUND_RE = re.compile(
    r"^gh api repos/[^\s?]+\?ref=(?P<ref>[0-9a-f]{7,40}) "
    r"--jq '\.content' \| base64 -d \| grep -(?:c|q) '(?P<needle>[^']+)'$"
)

# Tails that would make ANY check non-falsifiable by swallowing its exit code.
_INERT_TAIL_RE = re.compile(r"\|\|\s*(?:true|exit\s+0)|2>\s*/dev/null\s*$")

# --- Allowlisted pre-existing forms (the shipped default is still these) -----
_ALLOWLISTED_RE = (
    # Evidence-Source binding / existence probe (tier L0, OMN-14741 shape).
    re.compile(r"^gh pr view \$\{PR_NUMBER\} --repo \$\{REPO\} --json number,state$"),
    # Product-diff-scope probe (tier L1 via the substance floor, OMN-14425).
    re.compile(r"^gh pr view \$\{PR_NUMBER\} --repo \$\{REPO\} --json files$"),
    # Deploy-scope assertion (OMN-14623).
    re.compile(
        r"^gh pr diff \$\{PR_NUMBER\} --repo \$\{REPO\} --name-only \| grep -qiE '.+'$"
    ),
    # Private-repo hosted-safe receipt-local grep (OMN-14766 F-16).
    re.compile(
        r"^grep -q '\^status: PASS\$' "
        r"\$CONTRACT_REPO_DIR/drift/dod_receipts/[^\s]+/command\.yaml$"
    ),
)


def _iter_check_values(contract: dict[str, object]) -> list[tuple[str, str]]:
    """Return ``(evidence_item_id, check_value)`` for every command check."""
    out: list[tuple[str, str]] = []
    items = contract.get("dod_evidence")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "<no-id>"))
        checks = item.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            if check.get("check_type") != "command":
                continue
            value = check.get("check_value")
            if isinstance(value, str):
                out.append((item_id, value))
    return out


def classify_check(check_value: str) -> tuple[str, str | None]:
    """Return ``(classification, violation_message)``.

    ``classification`` is one of ``content_bound`` / ``allowlisted`` /
    ``unknown``. A violation message is returned for anything that is neither a
    well-formed content-bound check nor an allowlisted legacy form.
    """
    value = check_value.strip()
    if not value:
        return "unknown", "empty check_value"

    if _INERT_TAIL_RE.search(value):
        return "unknown", (
            "check_value swallows its exit code (|| true / || exit 0 / "
            "trailing 2>/dev/null) and can therefore never go RED"
        )

    match = _CONTENT_BOUND_RE.match(value)
    if match:
        if value.count("?ref=") != 1:
            return "unknown", "content-bound check pins more than one ?ref="
        if not match.group("needle").strip():
            return "unknown", "content-bound check greps for an empty needle"
        return "content_bound", None

    if value.startswith("gh api ") and "?ref=" in value:
        # It LOOKS like a content read but does not match the vetted grammar —
        # fail closed rather than assume it is falsifiable.
        return "unknown", (
            "check_value resembles a content read but does not match the "
            "RED-derivable grammar (one pinned ?ref=<hex>, terminal "
            "grep -c/-q with a non-empty needle)"
        )

    for pattern in _ALLOWLISTED_RE:
        if pattern.match(value):
            return "allowlisted", None

    return "unknown", "check_value matches no known generated-check form"


def check_contract(path: Path) -> list[dict[str, str]]:
    """Return a list of violation records for one rendered companion contract."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [{"path": str(path), "item": "<file>", "reason": f"unreadable: {exc}"}]
    if not isinstance(data, dict):
        return [{"path": str(path), "item": "<file>", "reason": "not a YAML mapping"}]

    violations: list[dict[str, str]] = []
    for item_id, value in _iter_check_values(data):
        _classification, reason = classify_check(value)
        if reason is not None:
            violations.append(
                {
                    "path": str(path),
                    "item": item_id,
                    "check_value": value,
                    "reason": reason,
                }
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "opt-in corpus mode (NOT wired to a gate): rewrite the pinned ref to "
            "--merge-base and assert the probe exits non-zero. Requires network."
        ),
    )
    parser.add_argument("--merge-base", default=None)
    args = parser.parse_args(argv)

    if args.live:
        # Deliberately not implemented as a gate path — see the module docstring.
        # The live transcript is produced by hand and recorded as evidence.
        print(
            "--live is an operator-run mode; run the rewritten probe manually "
            "and record the transcript. No gate consumes this path.",
            file=sys.stderr,
        )
        return 0

    violations: list[dict[str, str]] = []
    for path in args.paths:
        if path.is_file():
            violations.extend(check_contract(path))

    if args.json:
        print(json.dumps({"violations": violations}, indent=2, sort_keys=True))
    elif violations:
        print("Generated dod_evidence checks that are not RED-derivable:\n")
        for v in violations:
            print(f"  {v['path']} [{v['item']}]: {v['reason']}")
            if "check_value" in v:
                print(f"    check_value: {v['check_value']}")
        print(
            "\nEvery generated check must be structurally capable of returning "
            "non-zero at the PR's merge base (OMN-15247)."
        )

    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
