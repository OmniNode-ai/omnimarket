# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""No .gitignore pattern may exclude a tracked file inside the package (OMN-18859).

Workspace-mode image builds stage this repo as a git-stripped sibling copy
(``omnibase_infra/scripts/runtime_build/stage_workspace.sh`` rsyncs every
sibling without its ``.git`` directory). Hatchling applies ``.gitignore`` as
an exclude filter whether or not ``.git`` is present, so any pattern matching
a tracked file under ``src/omnimarket/`` silently drops that file from the
wheel while the staged source on disk still carries it. The OMN-14631
content-parity gate in ``compute_workspace_provenance.py`` then hard-fails
the build.

That happened on 2026-09-19. ``omnimarket#2670`` adopted the propagated
``public_repo_hygiene`` block, which declared a bare ``merge-sweep/``. A bare
directory pattern matches at ANY depth, so it also matched the real,
git-tracked package directory
``src/omnimarket/adapters/codex/skills/merge-sweep/``:

    $ git check-ignore --no-index -v \\
          src/omnimarket/adapters/codex/skills/merge-sweep/SKILL.md
    .gitignore:60:merge-sweep/  src/omnimarket/adapters/codex/skills/merge-sweep/SKILL.md

Every ``BUILD_SOURCE=workspace`` build failed from 21:19Z, and the .201 dev
lane could not rebuild at all while ``omninode-runtime`` crash-looped on a fix
it could not receive.

``--no-index`` is the whole subtlety and is why nothing caught this in review:
the file is TRACKED, so plain ``git check-ignore`` stays silent and the
pattern looks harmless in a clone. A git-stripped staged tree has no index to
consult, so the pattern applies there and only there.

The spec-side fix (anchoring the pattern, plus a test that every plain-name
directory pattern in the baseline is root-anchored) lives in omnibase_core.
This test is the repo-local half: it asserts the property that actually
matters HERE, over every tracked file in the package, so a future pattern —
propagated or hand-added — that swallows packaged source is caught in this
repo's own suite rather than by a dead lane.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = "src/omnimarket"


def _tracked_package_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", PACKAGE_DIR],
        cwd=REPO_ROOT,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def _ignored(paths: list[str]) -> list[str]:
    """Return the subset of ``paths`` this repo's .gitignore patterns match.

    ``--no-index`` makes git answer for the patterns alone, ignoring whether
    a path is tracked — which is exactly what a git-stripped staged tree
    presents to hatchling. Exit code 1 means "no path matched" and is the
    healthy outcome, so it is not an error.

    ``-z`` is required on BOTH sides and is not cosmetic: ``--stdin`` reads
    newline-separated paths unless ``-z`` is given, so feeding NUL-separated
    input without it makes git read the whole batch as one absurd path,
    match nothing, and return a confident empty result. That is the failure
    this helper exists to detect, so it would have reported a clean repo
    while source was being dropped. ``test_check_ignore_probe_is_live``
    below is the control that catches it.
    """
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=REPO_ROOT,
        env=scrub_git_location_env(os.environ),
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore failed (rc={result.returncode}): {result.stderr}"
        )
    return [path for path in result.stdout.split("\0") if path]


@pytest.mark.unit
def test_no_tracked_package_file_is_excluded_by_gitignore() -> None:
    """RED before the OMN-18859 anchor, GREEN after.

    Before the fix this returns
    ``src/omnimarket/adapters/codex/skills/merge-sweep/SKILL.md``.
    """
    tracked = _tracked_package_files()
    assert tracked, "expected tracked files under src/omnimarket"

    swallowed = _ignored(tracked)

    assert not swallowed, (
        "these tracked package files are matched by this repo's own .gitignore, "
        "so a git-stripped workspace wheel build silently drops them and the "
        "OMN-14631 content-parity gate hard-fails every image build: "
        f"{swallowed}. Anchor the offending pattern to the repo root "
        "(e.g. '/name/' rather than a bare 'name/'), which a bare directory "
        "pattern needs because it otherwise matches at any depth (OMN-18859). "
        "Run 'git check-ignore --no-index -v <path>' to see which line matched."
    )


@pytest.mark.unit
def test_check_ignore_probe_is_live() -> None:
    """Positive control for the test above.

    An empty result only means something if the probe can report a non-empty
    one. A path under a genuinely ignored directory must come back matched;
    otherwise the test above would pass on a broken probe and report a clean
    bill of health over a repo that is dropping source.

    The canary is deliberately batched WITH real tracked paths rather than
    probed alone. The first draft of this helper fed NUL-separated input
    without ``-z``, so git read the whole batch as a single path and matched
    nothing — and a single-path control still passed, because one path with
    no separator in it happens to survive that bug. Only a batched control
    distinguishes "nothing is ignored" from "the probe cannot read its own
    input".
    """
    canary = f"{PACKAGE_DIR}/__pycache__/omn18859_probe.pyc"
    batch = [*_tracked_package_files()[:50], canary]

    # Membership, not equality: this control answers "can the probe report a
    # match at all", and must stay green while the repo is broken so the two
    # failures above and here never get conflated into one cause.
    assert canary in _ignored(batch), (
        "the check-ignore probe did not report the canary inside a realistic "
        "batch, so a clean result from the parity test above would be "
        "meaningless rather than reassuring"
    )
