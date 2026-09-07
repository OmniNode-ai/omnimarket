# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A test may not hand-type the event type it matches on (OMN-18013).

THE DEFECT CLASS
----------------
The auto-wiring consume boundary stamps the ALIAS ``<producer>.<event-name>`` on an
inbound message (``handler_wiring._derive_event_type_from_topic`` /
``topic_constants.derive_event_type_alias_for_topic``). It never stamps the full topic
string. The dispatcher index happens to accept BOTH spellings as keys
(``derive_entry_message_types`` registers the literal subscribe topic AND the alias), so
a test that passes ``event_type="onex.evt.omnimarket.redeploy-completed.v1"`` is green on  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
a shape the bus does not carry -- and stays green when the alias the runtime really uses
stops resolving.

Live control 2026-09-06: ``onex.evt.omnimarket.delegate-skill-completed.v1`` offset 221
carried ``event_type='omnimarket.delegate-skill-completed'``.

THE RULE
--------
A test names the TOPIC and lets
:func:`omnimarket.testing.publisher_contract_fixture.publisher_event_type` derive the
event type from the corpus, which also proves some contract declares a PUBLISHER for it::

-   event_type="onex.evt.omnimarket.redeploy-completed.v1"  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
+   event_type=publisher_event_type("onex.evt.omnimarket.redeploy-completed.v1")  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin

No baseline, no allowlist. A topic the fixture cannot resolve is a hole in the contract
graph; fix the contract.
"""

from __future__ import annotations

import contextlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SCAN_ROOT = Path("tests")

# `event_type=` / `event_type:` / `"event_type":` immediately assigned a literal ONEX
# topic string. Deliberately narrow: it matches an ASSIGNMENT, not a mention, so a topic
# constant, a subscribe list or a docstring never trips it.
_PATTERN = re.compile(
    r"""(?P<field>\bevent_type\b|["']event_type["'])\s*[:=]\s*"""
    r"""(?P<quote>["'])(?P<topic>onex\.[A-Za-z0-9._-]+\.v\d+)(?P=quote)"""
)

# A scan that finds far fewer files than the tree has is a broken scan, not a clean tree.
DEFAULT_MIN_EXPECTED_FILES = 200

# ---------------------------------------------------------------------------------------
# THE FENCE, AND WHY IT IS NOT A BASELINE
# ---------------------------------------------------------------------------------------
# The operator ruling of 2026-09-06 (firm) is that these checks ship ENFORCED, not
# detected, and that a gate must not be re-freezable. Both halves are honoured here, and
# the difference from a baseline is mechanical, not rhetorical:
#
#   * a baseline is keyed on the DEFECT and grows whenever a new one appears. This is
#     keyed on the exact (path, topic) PAIR, and a pair that is no longer present is a
#     hard FAILURE (see `_stale`), so the list cannot rot and cannot be topped up by
#     re-running a writer -- there is no writer, and no flag that adds to it.
#   * every entry names an OWNER that is not this lane, and states what makes it go away.
#
# Two owners, and no third category is permitted:
#
#   PEER  The redeploy / prod-promotion FSM chain is live in another lane's worktree
#         (ledger CLAIM docs/tracking/ROLLING_WORK_LEDGER.md:3904, lane
#         dev-lane-fsm-residuals, tickets OMN-16939 / OMN-17888). Every one of these
#         topics DOES resolve through publisher_event_type today -- the conversion is
#         mechanical and is left to the lane holding the files, because two lanes editing
#         the same golden chain is how a merge silently drops an assertion.
#
#   HOLE  The topic has NO declared publisher anywhere in the corpus, so
#         publisher_event_type refuses it BY DESIGN: the contract graph has no such edge
#         and a golden chain written against it is green on a message the bus can never
#         carry. The fix is a contract, not a test edit -- and it is product work, because
#         the nodes that would publish these do not exist (the closeout chain's three
#         effect nodes, the pr-review FSM's phase-advance producer, the pattern-B
#         dispatch completion producer). Recorded as a residual on OMN-18013.
#
# The gate is WIRED AND REQUIRED with this fence in place, so every site outside it --
# and every new site anywhere, including in these same files -- fails.
_FENCED: tuple[tuple[str, str, str], ...] = (
    # -- PEER: dev-lane-fsm-residuals, ROLLING_WORK_LEDGER.md:3904 -----------------------
    (
        "tests/test_deploy_effect_prod_grant_refusal.py",
        "onex.cmd.omnimarket.redeploy-deploy-publish.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_deploy_effect_prod_grant_refusal.py",
        "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_golden_chain_image_built_dispatch.py",
        "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_golden_chain_image_built_dispatch.py",
        "onex.evt.omnimarket.runtime-image-built.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_golden_chain_prod_promotion_grant_resolver_effect.py",
        "onex.cmd.omnimarket.redeploy-start.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_golden_chain_prod_promotion_grant_resolver_effect.py",
        "onex.evt.omnimarket.prod-promotion-grant-resolved.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_golden_chain_redeploy.py",
        "onex.evt.omnimarket.redeploy-phase-advance.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_golden_chain_redeploy_orchestrator.py",
        "onex.cmd.omnimarket.redeploy-start.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_golden_chain_redeploy_orchestrator.py",
        "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_golden_chain_redeploy_orchestrator.py",
        "onex.evt.omnimarket.prod-promotion-grant-resolved.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_redeploy_input_boundary.py",
        "onex.cmd.omnimarket.redeploy-start.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_redeploy_input_boundary.py",
        "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_redeploy_prod_promotion_grant.py",
        "onex.cmd.omnimarket.redeploy-start.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_redeploy_prod_promotion_grant.py",
        "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_redeploy_prod_promotion_grant.py",
        "onex.evt.omnimarket.prod-promotion-grant-resolved.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    (
        "tests/test_redeploy_rollback.py",
        "onex.cmd.omnimarket.redeploy-deploy-publish.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "PEER",
    ),
    # -- HOLE: no contract in the corpus publishes the topic ----------------------------
    (
        "tests/test_codex_runtime_client.py",
        "onex.evt.omnimarket.pattern-b-dispatch-completed.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "HOLE",
    ),
    (
        "tests/test_golden_chain_pr_review.py",
        "onex.evt.omnimarket.pr-review-bot-phase-advance.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "HOLE",
    ),
    (
        "tests/test_golden_chain_runtime_closeout_orchestrator.py",
        "onex.evt.omnimarket.closeout-fitness-gated.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "HOLE",
    ),
    (
        "tests/test_golden_chain_runtime_closeout_orchestrator.py",
        "onex.evt.omnimarket.closeout-preflight-completed.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "HOLE",
    ),
    (
        "tests/test_golden_chain_runtime_closeout_orchestrator.py",
        "onex.evt.omnimarket.closeout-proof-matrix-completed.v1",  # onex-topic-allow: pinned fence row; reading it from the file it pins defeats the pin
        "HOLE",
    ),
)

_FENCED_PAIRS: frozenset[tuple[str, str]] = frozenset((p, t) for p, t, _ in _FENCED)


@dataclass(frozen=True, slots=True)  # internal-dataclass-ok: validator-internal finding
class LiteralEventType:
    """One test site feeding a literal topic string as an event type."""

    path: str
    line: int
    topic: str


def _norm(path: str) -> str:
    """Repo-relative, forward-slash form, so a fence entry is machine-comparable.

    The scan root may be given absolutely (a test, an editor integration) or relatively
    (the hook and the CI job, which run from the repo root). A fence keyed on one spelling
    and compared against the other silently matches nothing, which would turn every fenced
    site back into a failure and every entry into a stale one — so both are folded to the
    repo-relative form here rather than at each call site.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        with contextlib.suppress(ValueError):
            candidate = candidate.relative_to(Path.cwd())
    return candidate.as_posix()


def scan(root: Path) -> tuple[list[LiteralEventType], int]:
    findings: list[LiteralEventType] = []
    files = 0
    for path in sorted(root.rglob("*.py")):
        files += 1
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            match = _PATTERN.search(line)
            if match is not None:
                findings.append(
                    LiteralEventType(str(path), number, match.group("topic"))
                )
    return findings, files


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    root = Path(args[0]) if args else DEFAULT_SCAN_ROOT
    minimum = int(args[1]) if len(args) > 1 else DEFAULT_MIN_EXPECTED_FILES

    findings, files = scan(root)
    if files < minimum:
        sys.stderr.write(
            f"[no-literal-event-type-in-tests] FAIL (vacuity guard): only {files} python "
            f"file(s) found under {root} (expected >= {minimum}). A gate over a collapsed "
            f"set proves nothing.\n"
        )
        return 1

    fenced = [f for f in findings if (_norm(f.path), f.topic) in _FENCED_PAIRS]
    findings = [f for f in findings if (_norm(f.path), f.topic) not in _FENCED_PAIRS]

    stale = sorted(_FENCED_PAIRS - {(_norm(f.path), f.topic) for f in fenced})
    if stale:
        sys.stderr.write(
            f"[no-literal-event-type-in-tests] FAIL (stale fence): {len(stale)} fenced "
            f"pair(s) no longer occur. A fixed site still listed is how a fence rots into "
            f"an exemption list. Delete these entries from _FENCED (OMN-18013):\n"
        )
        for path, topic in stale:
            sys.stderr.write(f"  - {path}  {topic}\n")
        return 1

    if findings:
        sys.stderr.write(
            f"[no-literal-event-type-in-tests] FAIL: {len(findings)} test site(s) feed a "
            f"literal ONEX topic string as an event_type. The bus carries the ALIAS "
            f"<producer>.<event-name>, never the topic, so these assert on a shape the "
            f"runtime never produces (OMN-18013):\n"
        )
        for f in findings:
            sys.stderr.write(f"  - {f.path}:{f.line}  {f.topic}\n")
        sys.stderr.write(
            '\n  Fix: event_type=publisher_event_type("<the topic>") from '
            "omnimarket.testing.publisher_contract_fixture. It derives the alias the same "
            "way the runtime does and refuses a topic no contract publishes.\n"
        )
        return 1

    sys.stderr.write(
        f"[no-literal-event-type-in-tests] OK: {files} test file(s) scanned, 0 unfenced "
        f"hand-typed event types; {len(fenced)} fenced site(s) all still present "
        f"({sum(1 for _, _, r in _FENCED if r == 'PEER')} PEER, "
        f"{sum(1 for _, _, r in _FENCED if r == 'HOLE')} HOLE).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
