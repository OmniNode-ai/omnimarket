#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One-producer-per-contract structural guard (OMN-14285, WI-1 / S1d).

ENFORCEMENT RATCHET, not detection. Wired as a pre-commit hook and a CI gate.

Why this exists
---------------
Before OMN-14285 three OCC-companion writers coexisted — ``OccAutobindAdapter``,
``OccContractAdapter``, and the bespoke ``scaffold_occ_receipt.py`` — each with
its own inline receipt/contract YAML template. Divergent templates produced
divergent ``contract_sha256`` shapes and the pre-OMN-14255 head-SHA bug (the H1
double-authoring divergence). S1 converged them into ONE producer
(``OccCompanionEmitter``) whose deterministic companion bytes are rendered by ONE
pure seam (``occ_evidence_stamp``).

This guard makes that convergence *irreversible*: it fails CI if a SECOND OCC
companion-authoring surface reappears. The structural signal is the authoring
template itself — an OCC companion contract/receipt is a single multi-line string
literal carrying ``schema_version`` + ``contract_sha256`` + (``dod_evidence:`` or
``evidence_item_id:``). That literal is permitted in exactly ONE module (the
seam). Any other src module defining such a template is a resurrected writer.

This is the STRUCTURAL half of "one producer per contract". The PROVENANCE half
(mechanically rejecting a hand-authored companion regardless of who wrote it) is
OMN-14055 / S2 and is intentionally out of scope here.

What it checks (over ``src/omnimarket/**.py``)
----------------------------------------------
1. OCC companion authoring templates appear only in the sanctioned seam module.
2. Exactly one ``class OccCompanionEmitter`` definition exists.
3. The retired ``scaffold_occ_receipt.py`` bespoke writer has not reappeared
   anywhere in this repository.

The gate FAILS (exit 1) on any violation. There is no warn-only mode.

Usage:
    python scripts/ci/check_one_occ_producer.py            # enforce
    python scripts/ci/check_one_occ_producer.py --json     # JSON report
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "omnimarket"

# The single sanctioned home for OCC companion-authoring templates (the seam).
_SEAM_RELPATH = (
    "src/omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/occ_evidence_stamp.py"
)

# Markers that jointly identify an OCC companion contract/receipt authoring
# template. All three groups must co-occur *within a single string literal* so a
# module that merely READS receipts (field names scattered across code) is not
# flagged.
_MARKER_SCHEMA = "schema_version"
_MARKER_HASH = "contract_sha256"
_MARKER_BODY = ("dod_evidence:", "evidence_item_id:")


def _is_occ_template_literal(value: str) -> bool:
    if _MARKER_SCHEMA not in value or _MARKER_HASH not in value:
        return False
    return any(marker in value for marker in _MARKER_BODY)


def _iter_string_constants(tree: ast.AST) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def find_violations() -> list[str]:
    violations: list[str] = []
    emitter_class_defs: list[str] = []

    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - defensive
            violations.append(f"{rel}: could not parse ({exc})")
            continue

        # (1) OCC companion authoring templates only in the seam.
        if rel != _SEAM_RELPATH:
            for literal in _iter_string_constants(tree):
                if _is_occ_template_literal(literal):
                    violations.append(
                        f"{rel}: defines an OCC companion authoring template "
                        f"(schema_version + contract_sha256 + dod_evidence/"
                        f"evidence_item_id) outside the sanctioned seam "
                        f"({_SEAM_RELPATH}). Route all OCC companion rendering "
                        "through occ_evidence_stamp — do not re-introduce a "
                        "second producer (OMN-14285 / WI-1)."
                    )
                    break

        # (2) OccCompanionEmitter defined exactly once.
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OccCompanionEmitter":
                emitter_class_defs.append(rel)

    if len(emitter_class_defs) == 0:
        violations.append(
            "no `class OccCompanionEmitter` found under src/omnimarket — the "
            "single OCC producer is missing (OMN-14285)."
        )
    elif len(emitter_class_defs) > 1:
        joined = ", ".join(sorted(emitter_class_defs))
        violations.append(
            f"`class OccCompanionEmitter` is defined more than once ({joined}); "
            "there must be exactly one OCC producer surface (OMN-14285)."
        )

    # (3) The retired bespoke scaffold must not reappear anywhere in the repo.
    for path in _REPO_ROOT.rglob("scaffold_occ_receipt.py"):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        violations.append(
            f"{rel}: the bespoke `scaffold_occ_receipt.py` OCC writer was retired "
            "under OMN-14285 and must not reappear. Author companions through the "
            "canonical node producer (OccCompanionEmitter)."
        )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit a JSON report.")
    args = parser.parse_args(argv)

    violations = find_violations()

    if args.json:
        print(
            json.dumps({"violations": violations, "passed": not violations}, indent=2)
        )
    elif violations:
        print("One-producer-per-contract guard FAILED (OMN-14285):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
    else:
        print("One-producer-per-contract guard passed: single OCC producer intact.")

    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
