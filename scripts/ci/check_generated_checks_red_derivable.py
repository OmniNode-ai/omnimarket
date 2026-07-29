#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""RED-derivability gate for GENERATED OCC companion contracts (static + live).

OMN-15247 layer 2/3 of the acceptance bar
-----------------------------------------
OMN-15247's mechanical bar is: *"for every generated check, the same check run
against the PR's merge-base must return non-zero."* That bar is enforced in
three layers:

1. **Mint-time (runtime, fail-closed).** ``OccCompanionEmitter`` executes the
   selected probe at the evidence ref (must exit 0) and at the merge base (must
   exit non-zero) before writing it. See ``_derive_content_bound_check``.
2. **Static grammar mode (offline, deterministic, no network).** Every
   ``dod_evidence`` check_value in a rendered companion contract must match
   either the content-bound grammar or one of the explicitly allowlisted forms.
   Structural only — it proves the SHAPE could go RED, never that it did.
3. **Live replay mode** (``--live``). Re-executes each content-bound check at
   its pinned ref (must exit 0) AND at the recorded RED ref (must exit
   non-zero). Needs network + ``GH_TOKEN``.

Where this is wired (OMN-15317 — verify before trusting this paragraph)
-----------------------------------------------------------------------
Before OMN-15317 this script was invoked by **nothing**: the only reference to
it in ``occ-emitter-golden-gate.yml`` was its *unit test* in the pytest arg
list, and the only reference in ``.pre-commit-config.yaml`` was inside a
``files:`` trigger regex, which decides *when* the pytest hook runs and never
executes the script. A 222-line detector that no step invokes is advisory
(CLAUDE.md rule 5), which is what OMN-15317 was filed for.

It is now invoked by two executing steps in ``.github/workflows/
occ-emitter-golden-gate.yml`` — the existing blocking gate, not a new workflow:

* *static* over ``tests/fixtures/occ_red_derivable/companion/contracts/`` with
  ``--min-checks``/``--min-content-bound``, so a vacuous run (zero files, zero
  checks, or a producer that stopped emitting content-bound checks) FAILS
  instead of passing silently;
* *live* over the same corpus, executing both legs against real public
  omnimarket refs (the sidecar receipt under
  ``companion/drift/dod_receipts/`` carries the RED ref).

``tests/fixtures/occ_red_derivable/negative/`` holds the deliberately-bad
counterparts (a pre-flip ``pr_existence`` revert mint; a content-bound check
pinned where the symbol does not exist) that prove this gate goes RED — see
``TestNonVacuityFloors`` / ``TestLiveReplay``. They are NOT passed to the gate
steps, only to the tests.

None of those fixtures are free-floating YAML: ``tests/unit/scripts/
test_check_generated_checks_red_derivable.py`` re-renders every one through the
real producer seam (``occ_evidence_stamp.render_companion_contract`` /
``render_downstream_receipt``) and asserts byte equality, so they cannot drift
from what the producer actually emits.

Scope, stated honestly
----------------------
This gate runs over contracts THIS repo's producer renders (the fixtures above
plus any path passed on the command line). It is deliberately **not** run across
the merged ``onex_change_control`` corpus: that corpus is dominated by
pre-existing existence-probe contracts, so a corpus-wide fail-closed gate today
would reject essentially all traffic — the same reject-everything trap
documented by ``check_contract_substance_floor.py``'s ``GATE_SELF_REFERENTIAL``
kill switch (flag ON => 2,277/6,916 rejected, 98.4% of new contracts blocked).

The allowlisted forms below are still ACCEPTED rather than rejected, and that is
deliberate even though OMN-15317 flipped the producer default to
``content_bound``: a minted companion still carries the ``--json files``
diff-scope item (the OMN-14409 substance-floor carrier) and the private-repo
hosted-safe receipt grep (OMN-14766 F-16), and the ``pr_existence`` binding
remains explicitly selectable as the reversal path. What the gate enforces is
that (a) every content-bound check is structurally RED-derivable, (b) no third,
un-vetted shape appears, and (c) at least ``--min-content-bound`` content-bound
checks are actually present — which is the assertion that goes RED if the
default is flipped back.

Usage:
    python scripts/ci/check_generated_checks_red_derivable.py <contract.yaml>...
    python scripts/ci/check_generated_checks_red_derivable.py --json <paths>
    python scripts/ci/check_generated_checks_red_derivable.py --live \\
        --min-content-bound 1 <contract.yaml>...
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
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

# --- Allowlisted generated forms (OMN-15247 R21b) ----------------------------
#
# This tuple is the complete vocabulary this repo's producer is permitted to
# emit. The gate runs blocking in ``occ-emitter-golden-gate.yml`` plus
# pre-commit, so emitting anything else fails.
#
# WHAT R21b CHANGED, and why the previous revision of this list was worse than
# the problem it fixed. R21 narrowed this allowlist to a single new family --
#
#     gh api repos/${REPO}/pulls/${PR_NUMBER}/files --paginate --jq '.[].sha'
#       | grep -qE '^[0-9a-f]{40}$'          (and .status / .filename siblings)
#
# -- and REMOVED every prior form, on the grounds that the priors were
# inadmissible under the OMN-15309 predicate. The priors were indeed
# inadmissible. The replacement was VACUOUS: measured, it exits 0 against
# omnimarket#1, omnimarket#100, OCC#5418 and OCC#5436 alike, because every PR on
# GitHub that changes a file has 40-hex blob SHAs and GitHub file statuses. And
# because the OCC runner pre-substitutes ``${REPO}``/``${PR_NUMBER}`` with the
# repo/PR whose CI is executing, on the OCC companion's own Contract Compliance
# run those tokens resolve to the COMPANION -- so the check read the companion's
# own diff on the exact surface it was minted to satisfy.
#
# A ratchet whose only permitted vocabulary is a PR-existence probe does not
# ratchet toward proof, it ratchets the machine ONTO the vacuous shape while
# humans keep supplying the real evidence by hand. So R21b inverts it: the
# vacuous family is now a NAMED REJECTION (``_VACUOUS_PR_FILES_RE`` below), the
# pre-R21 provenance forms are restored as ACCEPTED-BUT-INERT, and the load-
# bearing addition is :data:`_ADMISSIBILITY_VALIDATOR_RE` -- an EXECUTED,
# FALSIFIABLE check against a surface the companion does not author, which
# ``require_admissibility_validator`` now asserts is present on every generated
# contract.
#
# Accepting an inert form is deliberate and is NOT the same as accepting a
# vacuous one. An inert value (``gh pr view ...``) is reported INERT/WARN by the
# OCC runner: it is visibly not proof, and the contract's admissibility comes
# from the validator item that explicitly SUPERSEDES it. A vacuous value reports
# PASS while proving nothing -- that is laundering, and it is what this list now
# rejects by name.
_ALLOWLISTED_RE = (
    # Binding provenance (pre-R21, restored). NOT_EXECUTED under the predicate;
    # reported INERT/WARN by the OCC runner, superseded by the validator item.
    re.compile(r"^gh pr view \$\{PR_NUMBER\} --repo \$\{REPO\} --json number,state$"),
    # Diff-scope provenance (pre-R21, restored). Same standing.
    re.compile(r"^gh pr view \$\{PR_NUMBER\} --repo \$\{REPO\} --json files$"),
    # Deploy-scope item (F-05 / OMN-14742), pre-R21 form restored. Carries the
    # literal ``deploy`` keyword the deploy-gate legacy substring rule greps for.
    re.compile(
        r"^gh pr diff \$\{PR_NUMBER\} --repo \$\{REPO\} --name-only \| "
        r"grep -qiE '[^']*deploy[^']*'$"
    ),
)

# The minted admissible check -- the byte-identical shape Codex's accepted
# hand-repairs appended to OCC#5406 / #5415 / #5418, now produced by the machine.
# Admissible SUBSTANTIVELY: ``uv`` is an executed hermetic command, it runs real
# behaviour, it goes RED when that behaviour breaks, and the file it names is one
# the companion does not author.
_ADMISSIBILITY_VALIDATOR_RE = re.compile(
    r"^uv run pytest tests/test_evidence_admissibility\.py -q$"
)

# --- Named anti-regression rules (OMN-15247 R21 / R21b) ----------------------
#
# The allowlist is exact-match, so these are strictly redundant for a conforming
# producer. They exist so the FAILURE MESSAGE names the property that was
# violated instead of the generic "matches no known form" -- the difference
# between a gate an author can act on and one they route around.
_SELF_REFERENTIAL_RE = re.compile(r"dod_receipts|\$\{?CONTRACT_REPO_DIR\b", re.I)

# R21b, the regression this round exists to prevent from recurring: a
# ``/pulls/<n>/files`` read, in EITHER placeholder or literal form, asserting only
# that the list is non-empty / well-formed. Green for every PR in existence, and
# self-referential on the OCC runner where the tokens resolve to the companion.
_VACUOUS_PR_FILES_RE = re.compile(
    r"gh api repos/\S+/pulls/(?:\$\{PR_NUMBER\}|\d+)/files\b"
)


def _iter_check_values(contract: dict[str, object]) -> list[tuple[str, str]]:
    """Return ``(evidence_item_id, check_value)`` for every GENERATED command check.

    OMN-15247 R21: items whose ``source`` is not ``generated`` are skipped. This
    gate's subject is the producer's own output (its name says so), and the
    admissible-only allowlist below is deliberately narrower than the OMN-15309
    predicate -- narrow enough that a legitimate HAND-authored repair (e.g. the
    ``source: manual`` ``uv run pytest ...`` item Codex appended to OCC#5406 and
    OCC#5415) would be rejected by it. Scoping to ``generated`` keeps the ratchet
    tight on the machine without turning it into a trap for humans; admissibility
    of hand-authored items is decided by the predicate in OCC, which is its job.
    """
    out: list[tuple[str, str]] = []
    items = contract.get("dod_evidence")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("source", "generated")).strip().lower() != "generated":
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

    ``classification`` is one of ``content_bound`` / ``admissibility_validator``
    / ``allowlisted`` / ``unknown``. A violation message is returned for anything
    that is none of those.
    """
    value = check_value.strip()
    if not value:
        return "unknown", "empty check_value"

    if _INERT_TAIL_RE.search(value):
        return "unknown", (
            "check_value swallows its exit code (|| true / || exit 0 / "
            "trailing 2>/dev/null) and can therefore never go RED"
        )

    # Named diagnoses, checked BEFORE the allowlist so the message names the
    # property that was violated rather than "unrecognised form".
    if _SELF_REFERENTIAL_RE.search(value):
        return "unknown", (
            "check_value reads back the receipt/contract tree this same "
            "companion PR authors (dod_receipts / $CONTRACT_REPO_DIR) -- "
            "INSIDE_OWN_DIFF under the OMN-15309 predicate, so it passes only "
            "because the producer typed the text it greps. Derive a "
            "content-bound pin against the PRODUCT repo, or declare the "
            "provenance form and let the admissibility-validator item supersede "
            "it"
        )
    if _VACUOUS_PR_FILES_RE.search(value):
        return "unknown", (
            "check_value reads `.../pulls/<n>/files` and asserts only that the "
            "list is non-empty or well-formed. MEASURED: that exits 0 for every "
            "PR on GitHub that changes at least one file, so it carries zero "
            "information about the change under test -- a PR-existence probe, "
            "which OMN-15247 names as a REJECTION class. It is also "
            "self-referential on the surface it runs on: the OCC compliance "
            "runner pre-substitutes ${REPO}/${PR_NUMBER} with the OCC COMPANION's "
            "own repo/number, so it reads the companion's own diff. Clearing the "
            "OMN-15309 predicate is a floor, not the goal"
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

    if _ADMISSIBILITY_VALIDATOR_RE.match(value):
        return "admissibility_validator", None

    for pattern in _ALLOWLISTED_RE:
        if pattern.match(value):
            return "allowlisted", None

    return "unknown", "check_value matches no known generated-check form"


def inspect_contract(
    path: Path,
) -> tuple[list[dict[str, str]], list[tuple[str, str]], int]:
    """Return ``(violations, content_bound_checks, inspected_check_count)``.

    ``content_bound_checks`` is ``(evidence_item_id, check_value)`` for every
    check that classified ``content_bound`` — the live-replay work list, and the
    population :func:`main`'s ``--min-content-bound`` floor counts.
    ``inspected_check_count`` includes allowlisted checks, since it answers "did
    this gate look at anything at all" (the ``--min-checks`` non-vacuity floor).
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        unreadable = {
            "path": str(path),
            "item": "<file>",
            "reason": f"unreadable: {exc}",
        }
        return [unreadable], [], 0
    if not isinstance(data, dict):
        not_mapping = {
            "path": str(path),
            "item": "<file>",
            "reason": "not a YAML mapping",
        }
        return [not_mapping], [], 0

    violations: list[dict[str, str]] = []
    content_bound: list[tuple[str, str]] = []
    checks = _iter_check_values(data)
    has_validator = False
    for item_id, value in checks:
        classification, reason = classify_check(value)
        if reason is not None:
            violations.append(
                {
                    "path": str(path),
                    "item": item_id,
                    "check_value": value,
                    "reason": reason,
                }
            )
        elif classification == "content_bound":
            content_bound.append((item_id, value.strip()))
        elif classification == "admissibility_validator":
            has_validator = True

    # OMN-15247 R21b -- the load-bearing assertion of this gate, and the one that
    # would have caught the original defect. Three consecutive companions
    # (OCC#5406 / #5415 / #5418) were born BLOCKED because OCC's
    # ``_has_effective_check`` found NOT ONE admissible check on them, and each
    # needed the same hand repair. A generated contract must therefore carry the
    # minted admissibility-validator item, which is the only generated form that
    # is admissible SUBSTANTIVELY (executed behaviour, falsifiable, against a file
    # the companion does not author) rather than by a spelling that dodges the
    # predicate's rules. Scoped to contracts that declare generated checks at all,
    # so a hand-authored contract is not dragged into the producer's ratchet.
    if checks and not has_validator:
        violations.append(
            {
                "path": str(path),
                "item": "<contract>",
                "check_value": "",
                "reason": (
                    "generated contract declares no admissibility-validator item "
                    "(check_value 'uv run pytest tests/test_evidence_admissibility"
                    ".py -q'). Without it every generated check here is inert or "
                    "provenance-only, OCC's _has_effective_check finds nothing "
                    "admissible, and the companion is born BLOCKED -- the exact "
                    "three-for-three failure OMN-15247 was filed for"
                ),
            }
        )
    return violations, content_bound, len(checks)


def check_contract(path: Path) -> list[dict[str, str]]:
    """Return a list of violation records for one rendered companion contract."""
    violations, _content_bound, _count = inspect_contract(path)
    return violations


# --- Live replay mode (OMN-15317) -------------------------------------------
#
# Layer 3. The static grammar above proves a check COULD go RED; this proves the
# committed check DOES: it re-executes the exact ``check_value`` at its pinned
# ref (must exit 0) and at the recorded RED ref (must exit non-zero). Both legs
# are required — asserting only the RED leg would credit a permanently broken
# probe (a deleted ref, a revoked scope, a typo'd path all exit non-zero at
# every ref), which is the same non-falsifiability this whole ticket is about,
# just inverted.

_PINNED_REF_RE = re.compile(r"\?ref=(?P<ref>[0-9a-f]{7,40})")


def run_probe(check_value: str, *, timeout: int = 60) -> tuple[str, int]:
    """Execute a probe and return ``(stdout, TRUE exit code)`` — never normalized.

    Mirrors ``OccCompanionEmitter._execute_probe_raw`` deliberately: the probe is
    a shell PIPELINE (``gh api … | base64 -d | grep -c …``), so without
    ``pipefail`` the status would be ``grep``'s alone and a failed ``gh api``
    would still report ``grep``'s verdict on empty input. A launch failure
    returns a non-zero sentinel, so an unrunnable probe is never read as passing.
    """
    try:
        result = subprocess.run(
            ["bash", "-o", "pipefail", "-c", check_value],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"probe launch failed: {exc}", 127
    return result.stdout.strip(), result.returncode


def run_green_leg(check_value: str, *, attempts: int = 2) -> tuple[str, int]:
    """Run the GREEN leg, retrying a non-zero result ``attempts-1`` times.

    Bounded retry on THIS leg only, and it does not weaken the assertion: a
    genuinely absent symbol fails every attempt, while a single GitHub 5xx would
    otherwise fail a merge on a fact that is true. The RED leg is never retried —
    its non-zero result is the PASS outcome, and a RED leg that exits 0 is a real
    non-falsifiability finding, not a transport question.
    """
    out, code = "", 1
    for _attempt in range(max(1, attempts)):
        out, code = run_probe(check_value)
        if code == 0:
            return out, code
    return out, code


def resolve_red_ref(
    contract_path: Path, item_id: str, *, explicit: str | None = None
) -> tuple[str | None, str]:
    """Resolve the RED (merge-base) ref for one content-bound check.

    ``explicit`` (``--merge-base``) wins. Otherwise the ref is read from the
    sidecar receipt the producer wrote next to the contract —
    ``<root>/drift/dod_receipts/<TICKET>/<item_id>/command.yaml`` — whose
    ``probe_stdout`` carries the mint-time derivation record
    ``{"evidence_ref":…,"green_exit":0,"red_ref":…,"red_exit":…}``. That is the
    real corpus shape; nothing is invented for the gate.

    Returns ``(ref_or_None, source_description)``. ``None`` is a VIOLATION at the
    call site, never a skip: a content-bound check whose RED ref cannot be
    recovered has no evidence that it is falsifiable.
    """
    if explicit:
        return explicit, "--merge-base"
    ticket = contract_path.stem
    receipt = (
        contract_path.parent.parent
        / "drift"
        / "dod_receipts"
        / ticket
        / item_id
        / "command.yaml"
    )
    if not receipt.is_file():
        return None, f"no sidecar receipt at {receipt}"
    try:
        data = yaml.safe_load(receipt.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, f"unreadable sidecar receipt {receipt}: {exc}"
    if not isinstance(data, dict):
        return None, f"sidecar receipt {receipt} is not a YAML mapping"
    try:
        derivation = json.loads(str(data.get("probe_stdout", "")).strip())
    except (TypeError, ValueError) as exc:
        return None, f"sidecar receipt {receipt} probe_stdout is not JSON: {exc}"
    red_ref = derivation.get("red_ref") if isinstance(derivation, dict) else None
    if not isinstance(red_ref, str) or not _PINNED_REF_RE.match(f"?ref={red_ref}"):
        return None, f"sidecar receipt {receipt} records no usable red_ref"
    return red_ref, str(receipt)


def replay_check_live(
    *,
    contract_path: Path,
    item_id: str,
    check_value: str,
    explicit_merge_base: str | None = None,
) -> dict[str, str] | None:
    """Execute both legs of one content-bound check. Returns a violation or None."""

    def _violation(reason: str) -> dict[str, str]:
        return {
            "path": str(contract_path),
            "item": item_id,
            "check_value": check_value,
            "reason": reason,
        }

    pinned = _PINNED_REF_RE.search(check_value)
    if pinned is None:  # pragma: no cover - grammar guarantees a pinned ref
        return _violation("live replay: no pinned ?ref=<hex> to rewrite")
    green_ref = pinned.group("ref")

    red_ref, source = resolve_red_ref(
        contract_path, item_id, explicit=explicit_merge_base
    )
    if red_ref is None:
        return _violation(f"live replay: RED ref unresolvable ({source})")
    if red_ref == green_ref:
        return _violation(
            f"live replay: RED ref equals the pinned ref ({green_ref[:8]}) — "
            "a probe cannot be falsified against the state it asserts"
        )

    green_out, green_exit = run_green_leg(check_value)
    if green_exit != 0:
        return _violation(
            f"live replay: GREEN leg failed at pinned ref {green_ref[:8]} "
            f"(exit {green_exit}) — the committed check does not hold at the ref "
            f"it pins: {green_out[:200]}"
        )

    red_check = check_value.replace(f"?ref={green_ref}", f"?ref={red_ref}")
    red_out, red_exit = run_probe(red_check)
    if red_exit == 0:
        return _violation(
            f"live replay: NON-FALSIFIABLE — the same check also passes at the "
            f"RED ref {red_ref[:8]} (exit 0, stdout {red_out[:80]!r}); RED ref "
            f"source: {source}"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "live replay: execute each content-bound check at its pinned ref "
            "(must exit 0) and at the RED ref from --merge-base or the sidecar "
            "receipt (must exit non-zero). Requires network + GH_TOKEN."
        ),
    )
    parser.add_argument(
        "--merge-base",
        default=None,
        help="RED ref for every replayed check; default reads the sidecar receipt.",
    )
    parser.add_argument(
        "--min-checks",
        type=int,
        default=0,
        help=(
            "fail if fewer than N command checks were inspected. The "
            "non-vacuity floor: a gate that inspects nothing must not report "
            "green (CLAUDE.md rule 5)."
        ),
    )
    parser.add_argument(
        "--min-content-bound",
        type=int,
        default=0,
        help=(
            "fail if fewer than N checks classify content_bound. Binds the gate "
            "to the OMN-15317 producer default: reverting the default to "
            "pr_existence makes the rendered fixtures carry zero content-bound "
            "checks and this floor goes RED."
        ),
    )
    args = parser.parse_args(argv)

    violations: list[dict[str, str]] = []
    inspected_files = 0
    inspected_checks = 0
    content_bound_total = 0
    for path in args.paths:
        if not path.is_file():
            continue
        inspected_files += 1
        file_violations, content_bound, file_check_count = inspect_contract(path)
        violations.extend(file_violations)
        inspected_checks += file_check_count
        content_bound_total += len(content_bound)

        if args.live:
            for item_id, check_value in content_bound:
                replay_violation = replay_check_live(
                    contract_path=path,
                    item_id=item_id,
                    check_value=check_value,
                    explicit_merge_base=args.merge_base,
                )
                if replay_violation is not None:
                    violations.append(replay_violation)

    if inspected_checks < args.min_checks:
        violations.append(
            {
                "path": ", ".join(str(p) for p in args.paths) or "<no paths>",
                "item": "<gate>",
                "reason": (
                    f"VACUOUS RUN: inspected {inspected_checks} command check(s) "
                    f"across {inspected_files} file(s), below the --min-checks "
                    f"floor of {args.min_checks}. A gate that inspects nothing "
                    "cannot report green."
                ),
            }
        )
    if content_bound_total < args.min_content_bound:
        violations.append(
            {
                "path": ", ".join(str(p) for p in args.paths) or "<no paths>",
                "item": "<gate>",
                "reason": (
                    f"inspected {content_bound_total} content-bound check(s), "
                    f"below the --min-content-bound floor of "
                    f"{args.min_content_bound}. Either the producer default was "
                    "reverted to the non-falsifiable pr_existence binding "
                    "(OMN-15317) or the fixtures no longer carry a content-bound "
                    "check."
                ),
            }
        )

    if args.json:
        print(
            json.dumps(
                {
                    "violations": violations,
                    "inspected_files": inspected_files,
                    "inspected_checks": inspected_checks,
                    "content_bound_checks": content_bound_total,
                    "live": args.live,
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif violations:
        print("Generated dod_evidence checks that are not RED-derivable:\n")
        for v in violations:
            print(f"  {v['path']} [{v['item']}]: {v['reason']}")
            if "check_value" in v:
                print(f"    check_value: {v['check_value']}")
        print(
            "\nEvery generated check must be structurally capable of returning "
            "non-zero at the PR's merge base (OMN-15247), and every content-bound "
            "check must actually do so on live replay (OMN-15317)."
        )
    else:
        mode = "live replay" if args.live else "static grammar"
        print(
            f"OK ({mode}): {inspected_checks} check(s) across {inspected_files} "
            f"contract(s), {content_bound_total} content-bound."
        )

    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
