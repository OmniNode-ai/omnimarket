#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Mint-side gate: the producer may not emit a companion that proves nothing.

OMN-15391
---------
A ``dod_evidence`` item has two independent surfaces — the **description** a
human reads and the **check_value** a machine runs — and nothing asserted they
describe the same proof. Every gate in the fleet reads only the command, and
every one asks "is this command real?", never "can this command's exit status
depend on the product change?". So a genuinely-executed, genuinely-falsifiable
command could stand in for a proof it has nothing to do with, pass every gate,
and prove nothing.

``node_dod_verify`` now refuses to count such a check toward completion
(``EnumEvidenceCheckStatus.NON_PROBATIVE``). That refusal is what neutralises
the merged corpus, which is not mass-edited. This gate is the other half: it
stops the producer minting NEW ones silently.

Two assertions, both mechanical:

**1. Producer/verifier agreement.** ``occ_evidence_stamp`` already carries its
own notion of which minted shapes observe the product —
``is_product_observing_check_value``, keyed on the ``?ref=`` content-pin marker
— and its own docstring says "everything else the producer can emit is
provenance". That notion and the verifier's
(``occ_evidence_probative_class.is_surrogate_check_value``) must be exact
complements over every shape the producer can mint. If they ever disagree, one
of two things has happened: the producer learned to mint a new shape that is
exit-status-invariant and nobody told the verifier (a new silent surrogate), or
the verifier is flagging something the producer knows is real (a false
positive). Both are defects, and both are caught here rather than discovered
later in a false-green ticket.

**2. No all-surrogate companion.** A rendered companion contract must carry at
least one check whose exit status can depend on the product change. This is the
mint-time mirror of the verify-side refusal: a companion that would be born
unable to prove anything is rejected at authoring time instead of merging and
then reading green forever.

Scope, stated honestly
----------------------
Assertion 2 runs over contracts THIS repo's producer renders (the
``tests/fixtures/occ_red_derivable`` corpus plus anything passed on the command
line). It is deliberately NOT run across the merged ``onex_change_control``
corpus — 347 of its 8,194 contracts have zero probative checks today, so a
corpus-wide fail-closed gate would reject essentially all traffic. That is the
same reject-everything trap ``check_contract_substance_floor.py`` documents,
and it is exactly why the merged corpus is neutralised on the VERIFY side
instead of being edited. Assertion 1 needs no corpus at all: it is a property
of the producer's own code.

Usage:
    python scripts/ci/check_minted_evidence_is_probative.py <contract.yaml>...
    python scripts/ci/check_minted_evidence_is_probative.py --agreement-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (  # noqa: E402
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ci_dod_evidence_check_value,
    downstream_dod_evidence_check_value,
    is_product_observing_check_value,
    self_bind_check_value,
)
from omnimarket.occ_evidence_probative_class import (  # noqa: E402
    classify_check_value,
    is_surrogate_check_value,
)


def _mintable_check_values() -> dict[str, str]:
    """Every distinct ``check_value`` shape the producer's public API emits.

    Built by CALLING the producer rather than by restating its strings, so a
    change to any of these functions is picked up here without an edit.
    """
    repo = "OmniNode-ai/omnibase_infra"
    content_bound = (
        "gh api repos/OmniNode-ai/omnibase_infra/contents/src/x.py"
        "?ref=bfa0b093646471667a265e4d884af53857fa2e10 --jq '.content' "
        "| base64 -d | grep -c 'def _require_str'"
    )
    return {
        "downstream (literal PR-pin fallback)": downstream_dod_evidence_check_value(
            pr_number=2692, repo=repo
        ),
        "downstream (content-bound override)": downstream_dod_evidence_check_value(
            pr_number=2692, repo=repo, content_bound_check_value=content_bound
        ),
        "ci diff-scope (literal fallback)": ci_dod_evidence_check_value(
            pr_number=2692, repo=repo
        ),
        "ci diff-scope (content-bound override)": ci_dod_evidence_check_value(
            pr_number=2692, repo=repo, content_bound_check_value=content_bound
        ),
        "self-bind": self_bind_check_value(
            occ_pr_number=6215, occ_repo="OmniNode-ai/onex_change_control"
        ),
        "admissibility validator": ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    }


def check_producer_verifier_agreement() -> list[str]:
    """Assertion 1. Returns a list of failure strings (empty == pass)."""
    failures: list[str] = []
    for label, check_value in _mintable_check_values().items():
        product_observing = is_product_observing_check_value(check_value)
        surrogate = is_surrogate_check_value(check_value)
        if product_observing == surrogate:
            failures.append(
                f"DISAGREEMENT [{label}]: the producer says "
                f"product_observing={product_observing} and the verifier says "
                f"surrogate={surrogate} — these must be exact complements. "
                f"check_value={check_value!r}. If the producer learned a new "
                "exit-status-invariant shape, register it in "
                "omnimarket.occ_evidence_probative_class so node_dod_verify "
                "refuses to count it; if the verifier is wrong, narrow the "
                "predicate. Do not silence this by editing only one side."
            )
    return failures


def check_contract_has_probative_evidence(path: Path) -> list[str]:
    """Assertion 2 for one rendered companion contract."""
    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [f"UNREADABLE [{path}]: {type(exc).__name__}: {exc}"]
    if not isinstance(document, dict):
        return [f"UNREADABLE [{path}]: top level is not a mapping"]

    items = document.get("dod_evidence")
    if not isinstance(items, list) or not items:
        return [f"NO_EVIDENCE [{path}]: contract declares no dod_evidence items"]

    surrogates: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for check in item.get("checks") or []:
            if not isinstance(check, dict):
                continue
            check_value = check.get("check_value")
            if not isinstance(check_value, str):
                continue
            if is_surrogate_check_value(check_value):
                surrogates.append(
                    f"{item.get('id')}: {classify_check_value(check_value).value}"
                )
            else:
                # One probative check is enough — the contract can bear a
                # verdict, and the surrogates beside it are additive provenance.
                return []

    if not surrogates:
        # Items exist but not one of them yielded a classifiable command: every
        # item declared no ``checks``, or every ``check_value`` was absent or
        # not a string. Reporting ALL_SURROGATE here would name a class that
        # was never observed and print an empty list as its evidence — the
        # unfalsifiable-diagnosis shape this ticket exists to remove. It is
        # still a failure: a contract with no runnable check cannot prove
        # completion either. It just gets the accurate reason.
        return [
            f"NO_CLASSIFIABLE_CHECK [{path}]: the contract declares "
            f"{len(items)} dod_evidence item(s) but not one of them carries a "
            "string check_value, so nothing can be executed and nothing can "
            "prove completion. Declare a check whose exit status depends on "
            "the product change."
        ]

    return [
        f"ALL_SURROGATE [{path}]: every check in this contract is "
        "exit-status-invariant over the product change, so the contract is "
        "green before the fix, after it, and with it reverted. It can never "
        f"prove completion. Non-probative checks: {'; '.join(surrogates)}. "
        "Bind a content-bound read at a pinned ref, or a test this change "
        "makes pass."
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--agreement-only",
        action="store_true",
        help="Run only the producer/verifier agreement assertion.",
    )
    args = parser.parse_args()

    failures = check_producer_verifier_agreement()
    checked = 0
    if not args.agreement_only:
        for path in args.paths:
            if path.is_dir():
                # A companion tree holds contracts AND their sidecar receipts;
                # only the former declare ``dod_evidence``. Restricting to the
                # ``contracts/`` segment keeps a directory argument meaning the
                # same thing it means for the sibling RED-derivability gate.
                candidates = [
                    found
                    for found in sorted(path.rglob("*.yaml"))
                    if "contracts" in found.parts
                ]
            else:
                candidates = [path]
            for candidate in candidates:
                checked += 1
                failures.extend(check_contract_has_probative_evidence(candidate))
        if args.paths and checked == 0:
            failures.append(
                "VACUOUS_RUN: paths were supplied but matched no contract — a "
                "gate that inspects nothing must fail rather than pass."
            )

    if failures:
        print("OMN-15391 minted-evidence gate: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        "OMN-15391 minted-evidence gate: PASS "
        f"(producer/verifier agreement over {len(_mintable_check_values())} "
        f"minted shapes; {checked} contract(s) carry probative evidence)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
