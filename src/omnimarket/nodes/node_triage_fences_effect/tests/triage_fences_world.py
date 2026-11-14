# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Constants of the triage-fences tests, copied verbatim from the frozen repository's tests.

``OMN_15954_LEDGER_LINE`` is the dangling CLAIM row a 2026-09-03 triage lane missed, byte for byte;
a paraphrase would stop reproducing the defect. Only the name of one private repository inside the
row is replaced by words, which the row's grammar does not read. ``NOW`` is the clock the goldens age claims against.
"""

from __future__ import annotations

from datetime import UTC, datetime

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)

OMN_15954_LEDGER_LINE = (
    "- 2026-09-01T18:42:53Z CLAIM lane=omn17349-staging-delivery actor=claude "
    "ticket=OMN-17349,OMN-15954 scope=omnimarket-release-v0.4.15+onex-api-pin-bump"
    "+staging-deploy-verify intent=deliver merged fix 0b17e0eb (omnimarket#2254) "
    "to staging: cut omnimarket v0.4.15 from dev tip 627f16b3 via the repo's own "
    "release.yml tag-push path (ancestry precondition verified: origin/main IS "
    "ancestor of origin/dev; tag-absent proven; pyproject at tip = 0.4.15), watch "
    "release run + PyPI readback, bump docker/onex-api/requirements.txt "
    "omnimarket==0.4.10->0.4.15 in the infra repository via governed worktree PR (no "
    "bypass, no PREPUSH overrides), let the sanctioned build-and-push-onex-api -> "
    "workflow_run -> Deploy onex-staging chain carry the pin, then live-verify the "
    "installed omnimarket version in the onex-api pod read-only. HARD FENCE "
    "HONOURED: never touches RUNTIME_REPIN_RELEASED or any repin/hold flag "
    "(omn16493 repin lane owns that surface); never cancels/reruns another actor's "
    "runs; waits out any in-flight Deploy onex-staging run before relying on a new "
    "one. Adoption check done: v0.4.11/v0.4.12 predate the fix merge (NOT "
    "ancestors), no release run in-flight, no open PR bumps the pin, 17:40Z "
    "collaborator-b-status TERMINAL confirms release-chain-unstarted. Cost: est ~2 "
    "lane-hours - 1 tag push, 1 release-run watch, 1 infra repository PR, 1 "
    "auto-triggered deploy watch, 2 Linear comments; displaces nothing - no active "
    "CLAIM names the omnimarket release cut, the onex-api pin, or OMN-17349 "
    "staging delivery."
)


__all__ = ["NOW", "OMN_15954_LEDGER_LINE"]
