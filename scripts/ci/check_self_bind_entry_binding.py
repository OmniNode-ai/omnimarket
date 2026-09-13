#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every minted OCC self-bind is per-entry bound (OMN-18304).

WHY THIS IS A GATE AND NOT A TEST. The producer's own mint-verify
(``OccCompanionEmitter._assert_self_bind_landed``) fires at mint time, on the
dev-lane runtime, against a live GitHub round-trip. It cannot fire on a pull
request that changes the rendering seam, so a change that reintroduces a
whole-file-only self-bind is invisible until a real companion is minted —
which is exactly how the defect reached ``onex_change_control#9316``.

WHAT IT REFUSES, stated as the invariant rather than as a file list:

1. **Rendered shape.** The self-bind receipt renderer must emit a
   ``contract_entry_sha256`` slot. The rebinder substitutes over an existing
   line; it does not insert one. A renderer that omits the slot produces a
   receipt the rebinder silently leaves whole-file-only, with no error
   anywhere — the measured failure.
2. **End-to-end binding.** Rendering the self-bind item and the self-bind
   receipt and running the real rebinder against the real contract must yield
   a receipt whose ``contract_entry_sha256`` equals the canonical per-entry
   hash. The expected value is recomputed with
   ``omnibase_core.validation.validator_receipt_gate.compute_contract_entry_sha256``
   — the same function the consumer gates use, never a local reimplementation.
3. **Append survival.** A sibling companion appending its own rows to the same
   contract must not move the first companion's binding. This is the property
   the whole-file pin lacked, and asserting it here is what makes the gate
   about the defect rather than about a field name.
4. **Single write path.** No source file may write a receipt under
   ``drift/occ_bindings/``. Already-merged ones on the OCC governance ref are
   untouched and keep resolving through the structural branch that still
   exists in core; what is refused is MINTING a new one.

POSITIVE CONTROL. ``--self-test`` runs the same four checks against a
deliberately broken renderer that drops the per-entry slot, and fails if they
report clean. An empty finding list from a check that cannot fail is not
evidence (rule 16). Exit 0 means the control produced the findings it should.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path

import yaml
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    compute_contract_sha256,
    rebind_contract_entry_sha256_in_text,
    rebind_contract_sha256_in_text,
    render_self_bind_dod_evidence_item,
    render_self_bind_receipt,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPO_ROOT / "src"
_TICKET = "OMN-0000"
_OCC_REPO = "OmniNode-ai/onex_change_control"

#: A source line that writes into the retired structural-binding tree. Matched
#: on the literal path segment inside a QUOTED string rather than on a Python
#: identifier, so a renamed constant cannot smuggle the write back in.
#:
#: The quoting requirement is not cosmetic. A matcher that fired on the bare
#: path also fired on the prose explaining why the path is retired — rule 15's
#: shape of a gate tripping on documentation about itself, hit while writing
#: this file. The correct repair is to narrow the matcher to what it means to
#: refuse (a write), never to add an exemption annotation for the prose.
_STRUCTURAL_WRITE_RE = re.compile(r"[\"'][^\"'\n]*drift/occ_bindings")

#: The unrebound sentinel. Shipping it is indistinguishable from shipping no
#: binding at all, so it is a finding in its own right.
_PENDING = "sha256:PENDING"

_SelfBindRenderer = Callable[..., str]


def _render_receipt(occ_pr_number: int, renderer: _SelfBindRenderer) -> str:
    return renderer(
        ticket_id=_TICKET,
        evidence_id=f"occ-self-bind-pr-{occ_pr_number}",
        occ_pr_number=occ_pr_number,
        occ_repo=_OCC_REPO,
        run_timestamp="2026-01-01T00:00:00Z",
        occ_commit_sha="0" * 40,
        branch="auto/gate-probe-occ-autobind",
        probe_command=(
            f"gh api repos/{_OCC_REPO}/pulls/{occ_pr_number}/files "
            "--paginate --jq '.[].sha'"
        ),
        probe_stdout="0" * 40,
        exit_code=0,
    )


def _render_contract(*occ_pr_numbers: int) -> str:
    head = (
        "---\n"
        'schema_version: "1.0.0"\n'
        f'ticket_id: "{_TICKET}"\n'
        'title: "OCC self-bind entry-binding gate probe"\n'
        "dod_evidence:\n"
    )
    return head + "".join(
        render_self_bind_dod_evidence_item(
            evidence_id=f"occ-self-bind-pr-{number}",
            occ_pr_number=number,
            occ_repo=_OCC_REPO,
            ticket_id=_TICKET,
        )
        for number in occ_pr_numbers
    )


def _rebind(receipt_text: str, contract_text: str, evidence_id: str) -> str:
    """Apply the producer's own rebind pass, in the producer's own order."""
    bound = rebind_contract_sha256_in_text(
        receipt_text, compute_contract_sha256(contract_text.encode("utf-8"))
    )
    try:
        entry_digest = compute_contract_entry_sha256(
            yaml.safe_load(contract_text), evidence_id
        )
    except Exception:
        return bound
    return rebind_contract_entry_sha256_in_text(bound, entry_digest)


def _check_rendered_shape(renderer: _SelfBindRenderer) -> list[str]:
    rendered = _render_receipt(9316, renderer)
    if "contract_entry_sha256" not in rendered:
        return [
            "the self-bind receipt renderer emits no contract_entry_sha256 "
            "slot; the rebinder substitutes over an existing line and cannot "
            "insert one, so every minted self-bind would ship whole-file-only "
            "(OMN-18304, live on onex_change_control#9316)"
        ]
    return []


def _check_end_to_end_binding(renderer: _SelfBindRenderer) -> list[str]:
    contract = _render_contract(9316)
    receipt = yaml.safe_load(
        _rebind(_render_receipt(9316, renderer), contract, "occ-self-bind-pr-9316")
    )
    expected = compute_contract_entry_sha256(
        yaml.safe_load(contract), "occ-self-bind-pr-9316"
    )
    actual = receipt.get("contract_entry_sha256")
    if actual is None:
        return ["the minted self-bind receipt carries no contract_entry_sha256"]
    if actual == _PENDING:
        return [
            "the minted self-bind receipt still carries the unrebound "
            f"{_PENDING!r} sentinel; the item is probably declared AFTER the "
            "rebind pass instead of before it"
        ]
    if actual != expected:
        return [
            f"the minted self-bind receipt binds {actual!r} but the canonical "
            f"per-entry hasher computes {expected!r}"
        ]
    return []


def _check_append_survival(renderer: _SelfBindRenderer) -> list[str]:
    first_contract = _render_contract(9316)
    receipt = yaml.safe_load(
        _rebind(
            _render_receipt(9316, renderer), first_contract, "occ-self-bind-pr-9316"
        )
    )
    second_contract = _render_contract(9316, 9320)
    if compute_contract_sha256(
        first_contract.encode("utf-8")
    ) == compute_contract_sha256(second_contract.encode("utf-8")):
        return [
            "gate self-check failed: the sibling-append probe did not change "
            "the contract, so this check cannot fail and proves nothing"
        ]
    expected = compute_contract_entry_sha256(
        yaml.safe_load(second_contract), "occ-self-bind-pr-9316"
    )
    if receipt.get("contract_entry_sha256") != expected:
        return [
            "a sibling companion's append to the same contract invalidated the "
            "first companion's self-bind binding — the OMN-17341 race "
            "(onex_change_control#9316 / #9320 / #9321)"
        ]
    return []


def _check_no_structural_write_path() -> list[str]:
    findings: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _STRUCTURAL_WRITE_RE.search(line):
                findings.append(
                    f"{path.relative_to(_REPO_ROOT)}:{number} mints into the "
                    "retired drift/occ_bindings tree; a new self-bind belongs "
                    "at drift/dod_receipts where its per-entry hash resolves"
                )
    return findings


def _run(renderer: _SelfBindRenderer, *, scan_sources: bool) -> list[str]:
    findings = _check_rendered_shape(renderer)
    findings += _check_end_to_end_binding(renderer)
    findings += _check_append_survival(renderer)
    if scan_sources:
        findings += _check_no_structural_write_path()
    return findings


def _broken_renderer(**kwargs: object) -> str:
    """A renderer that drops the per-entry slot — the pre-OMN-18304 shape."""
    rendered = render_self_bind_receipt(**kwargs)  # type: ignore[arg-type]
    return "\n".join(
        line
        for line in rendered.splitlines(keepends=True)
        if not line.startswith("contract_entry_sha256:")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "positive control: run the binding checks against a renderer known "
            "to drop the per-entry slot and fail if they report clean"
        ),
    )
    args = parser.parse_args(argv)

    if args.self_test:
        control = _run(_broken_renderer, scan_sources=False)
        if not control:
            print(
                "POSITIVE CONTROL FAILED: the binding checks reported clean "
                "against a renderer that drops contract_entry_sha256, so a "
                "clean run of this gate proves nothing.",
                file=sys.stderr,
            )
            return 1
        print(
            "positive control OK: "
            f"{len(control)} finding(s) against the broken renderer."
        )
        return 0

    findings = _run(render_self_bind_receipt, scan_sources=True)
    if findings:
        print("OCC self-bind entry-binding gate FAILED (OMN-18304):", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("OCC self-bind entry-binding gate OK: one shape, per-entry bound.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
