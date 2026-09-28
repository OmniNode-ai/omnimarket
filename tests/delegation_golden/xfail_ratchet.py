# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shrink-only ratchet over the delegation golden corpus's `xfail` set (OMN-19446).

Mirrors the RATCHET SEMANTICS already shipped for
``omnimarket.validators.handler_dispatch_entrypoint``
(``validation/handler_dispatch_entrypoint_baseline.yaml``): a frozen,
committed baseline names every case allowed to carry `xfail` today, keyed by
case id and naming a tracking ticket. The corpus's live xfail set may only
ever be a SUBSET of the baseline -- a case newly marked xfail with no baseline
entry is refused, and a baseline entry whose case is no longer xfailed is
refused as stale. The baseline can only shrink; its end state is empty.

Usable as a pytest module (``test_xfail_ratchet_omn19446.py``) and standalone
(pre-commit / CI, or a nightly step that prints the live count):

    uv run python -m tests.delegation_golden.xfail_ratchet
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_BASELINE_PATH = Path(__file__).parent / "xfail_baseline.yaml"


class ModelXfailBaselineEntry(BaseModel):
    """One frozen, allowed xfail instance: a case id and its tracking ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., min_length=1)
    ticket: str = Field(..., pattern=r"^OMN-\d+$")


def load_baseline(baseline_path: Path = DEFAULT_BASELINE_PATH) -> frozenset[str]:
    """Load the frozen shrink-only baseline's case ids.

    Raises if the file is missing/malformed or an entry lacks `ticket` --
    unlike ``handler_dispatch_entrypoint.load_baseline`` (which tolerates an
    absent file as "empty baseline", because an absent burn-down list there
    just means nothing is known-broken yet), an absent xfail baseline here
    would silently make every live xfail a violation with no route to
    intentionally allow one, which is a worse failure mode for this gate: fail
    loudly on a missing/malformed baseline rather than mysteriously red.
    """
    if not baseline_path.is_file():
        raise FileNotFoundError(
            f"{baseline_path} is missing. The shrink-only xfail baseline must "
            "be a committed file naming every case currently allowed to carry "
            "xfail, each with a tracking ticket."
        )
    raw = yaml.safe_load(baseline_path.read_text()) or {}
    rows = raw.get("known_xfail") or []
    entries = [ModelXfailBaselineEntry.model_validate(row) for row in rows]
    return frozenset(entry.case_id for entry in entries)


def ratchet_violations(
    live_xfail_ids: frozenset[str],
    baseline_ids: frozenset[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Return (violations, stale).

    violations: live xfail ids with no baseline entry (new instance -- refused).
    stale:      baseline ids no longer xfailed live (must shrink -- refused).
    """
    return (live_xfail_ids - baseline_ids, baseline_ids - live_xfail_ids)


def main(argv: list[str] | None = None) -> int:
    from tests.delegation_golden.corpus_loader import load_corpus

    corpus = load_corpus()
    live_xfail_ids = frozenset(c.id for c in corpus.cases if c.xfail is not None)
    baseline_ids = load_baseline(DEFAULT_BASELINE_PATH)
    violations, stale = ratchet_violations(live_xfail_ids, baseline_ids)

    exit_code = 0
    if violations:
        exit_code = 1
        sys.stderr.write(
            "[xfail-ratchet] FAIL: corpus case(s) carry xfail with no baseline "
            f"entry: {sorted(violations)}. Add them to {DEFAULT_BASELINE_PATH} "
            "naming a tracking ticket, reviewed in the same PR.\n"
        )
    if stale:
        exit_code = 1
        sys.stderr.write(
            f"[xfail-ratchet] FAIL: baseline entries {sorted(stale)} name "
            "case(s) no longer xfailed in corpus.yaml. Remove them -- the "
            f"baseline is shrink-only: {DEFAULT_BASELINE_PATH}.\n"
        )
    if exit_code == 0:
        sys.stderr.write(
            f"[xfail-ratchet] OK: {len(live_xfail_ids)} case(s) carry xfail "
            f"({sorted(live_xfail_ids)}), all in the frozen baseline, 0 stale "
            "entries.\n"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
