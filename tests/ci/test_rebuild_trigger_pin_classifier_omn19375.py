# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19375 / OMN-19378: the rebuild trigger's pin carries the classifier fixes.

``runtime-rebuild-trigger.yml`` runs omnibase_infra's runtime-affecting
classifier at the pinned commit (the reusable checks its scripts out at
``github.job_workflow_sha``). Below omnibase_infra 89d4dd6cf that classifier is
wrong about this repository in both directions, each measured on 2026-09-24:

* It under-counts. omnimarket#2813 (``src/omnimarket/events/``) and #2817
  (``src/omnimarket/delegation/``) read "No rebuild trigger", although the lane
  installs all of ``src/omnimarket`` from the staged clone.
* It over-counts. Every post-release bot PR, which changes only this
  repository's own version in ``pyproject.toml`` and ``uv.lock``, read runtime
  and rebuilt the dev lane about 12 minutes after each merge (jobs
  ``0805d076``, ``e4d36317`` and ``ff4dc5de``; the last preceded the C15 failure
  at 07:51Z).

A pin revert below 89d4dd6cf reopens both with every other test in this
directory green, so it is refused here by name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "runtime-rebuild-trigger.yml"
REUSABLE = (
    "OmniNode-ai/omnibase_infra/.github/workflows/runtime-rebuild-trigger-reusable.yml"
)

#: omnibase_infra#4057, the commit that carries both fixes.
_CLASSIFIER_FIX_COMMIT: Final[str] = "89d4dd6cf"

#: Pins recorded as carrying ``_CLASSIFIER_FIX_COMMIT``, each with its evidence.
#: Add a pin here only after `git merge-base --is-ancestor 89d4dd6cf <pin>`
#: exits 0 in an omnibase_infra clone.
_PINS_CARRYING_THE_CLASSIFIER_FIX: Final[dict[str, str]] = {
    "89d4dd6cffaef2137c792cbec81d170341df65d2": (
        "omnibase_infra#4057 (OMN-19375) itself, merged 2026-09-24T12:12:04Z"
    ),
}

_FULL_SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")


def _rebuild_job() -> dict[str, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}
    matching = [
        job
        for job in jobs.values()
        if isinstance(job, dict) and REUSABLE in str(job.get("uses") or "")
    ]
    assert len(matching) == 1, f"expected exactly one caller of {REUSABLE}"
    return matching[0]


def test_the_pin_carries_the_classifier_fixes() -> None:
    """RED at any pin below 89d4dd6cf, including the 3a7813cff it replaces."""
    uses = str(_rebuild_job()["uses"])
    _, _, ref = uses.partition("@")
    assert _FULL_SHA.match(ref), f"`uses: ...@{ref}` is not a full commit SHA"
    assert ref in _PINS_CARRYING_THE_CLASSIFIER_FIX, (
        f"the reusable is pinned to {ref}, which is not recorded as carrying "
        f"omnibase_infra {_CLASSIFIER_FIX_COMMIT} (OMN-19375). Below it, a "
        "src/omnimarket change can read No rebuild and every post-release "
        "version bump rebuilds the dev lane. If the new pin does carry it, prove "
        f"it with `git merge-base --is-ancestor {_CLASSIFIER_FIX_COMMIT} {ref}` "
        "and add it to _PINS_CARRYING_THE_CLASSIFIER_FIX with that evidence."
    )


def test_the_caller_passes_the_repo_slug_the_manifest_checkout_reads() -> None:
    """The version-bump exemption checks out ``repo_slug`` at ``source_sha``.

    Without both, the reusable's sparse checkout of this merge commit cannot
    run, and every bump stays runtime (the classifier fails closed).
    """
    inputs = _rebuild_job().get("with") or {}
    assert inputs.get("repo_slug") == "OmniNode-ai/omnimarket"
    assert "merge_commit_sha" in str(inputs.get("source_sha"))
