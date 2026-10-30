# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Keep hook-subprocess tests from spending a real lab host's cores (OMN-16991).

Several tests in this directory run the REAL pre-push hook with the heavy
escalation forced (``PREPUSH_FULL_SUITE=1``) and every designated host
de-designated, to prove the refusal. Before OMN-17435 that was harmless in this
repo for a structural reason: the hook had no lab-dispatch seam at all, so there
was no remote host it could reach.

This port removes that accidental containment. The equivalent was observed live
in omnibase_infra on 2026-08-30, minutes after its scan was fixed: `pytest`
shipped a real git bundle to ``h105``, took that host's exclusive slot, and
started a full suite there -- with ORIGIN on the remote wrapper naming the test
process itself. That is the OMN-16425/OMN-16489 F-01 recursion in its
distributed form, reached from a unit test instead of a push, and it burns a lab
host for as long as the suite runs.

The isolation below uses the picker's OWN deterministic override surface rather
than a new knob. ``PREPUSH_SLOT_OVERRIDE_MAP`` is consulted before any network
call, and a label absent from the map resolves to "slot unknown", which the
picker treats as unfit and skips -- the same fail-closed posture it applies to
an unreachable host. A map naming no real label therefore makes EVERY row unfit
with zero ssh, and stays correct when a row is added to
the private deployment table.

It can only make the gate stricter. With no host placeable the lab leg produces
no evidence and the hook falls through to its pre-existing precedence (grant ->
die), which is exactly what these tests assert.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from tests.scripts._prepush_private_table import SYNTHETIC_TABLE, write_private_root


@functools.lru_cache(maxsize=1)
def synthetic_config_root() -> Path:
    base = Path(tempfile.mkdtemp(prefix="prepush-test-config-"))
    return write_private_root(
        base, SYNTHETIC_TABLE.replace("/srv/onex-prepush", str(base / "slots"))
    )


#: Deliberately names no real row label. See the module docstring.
#:
#: ``PREPUSH_REACH_OVERRIDE_MAP`` (OMN-17280) closes the second network surface
#: this module exists to close. The same-host route probes lab reachability
#: with a real ``ssh ... true`` before it may fire, and a hook-subprocess test
#: that reached a designated row would otherwise open real connections from
#: pytest. ``default=up`` reports EVERY row -- including rows added later --
#: as reachable, which makes the same-host route DECLINE. That is the strict
#: direction: the leg produces no evidence and the hook falls through to its
#: pre-existing precedence, which is exactly what these tests assert.
LAB_ISOLATION_ENV = {
    "PREPUSH_SLOT_OVERRIDE_MAP": "no-such-host=unknown",
    "PREPUSH_REACH_OVERRIDE_MAP": "default=up",
}


def network_free_lab_env() -> dict[str, str]:
    """Env fragment that makes the lab-dispatch leg network-free."""
    return {
        **LAB_ISOLATION_ENV,
        "ONEX_WORKSPACE_CONFIG_ROOT": str(synthetic_config_root()),
    }


@functools.lru_cache(maxsize=1)
def hook_test_root() -> Path:
    """A committed fixture tree for whole-hook subprocess tests."""
    source = Path(__file__).resolve().parents[2]
    root = Path(tempfile.mkdtemp(prefix="prepush-hook-tree-"))
    for directory in ("scripts/hooks", "scripts/ci"):
        shutil.copytree(
            source / directory,
            root / directory,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    for filename in ("pyproject.toml", "uv.lock"):
        shutil.copy2(source / filename, root / filename)
    subprocess.run(
        ["git", "init", "-q", str(root)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "."],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Synthetic hook fixture",
        ],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return root


def hook_test_env() -> dict[str, str]:
    """Use the existing test interpreter without syncing a fixture project."""
    return {
        "UV_PROJECT_ENVIRONMENT": str(Path(sys.executable).parent.parent),
        "UV_NO_SYNC": "1",
        "UV_PYTHON": sys.executable,
        "GIT_DIR": str(hook_test_root() / ".git"),
        "GIT_WORK_TREE": str(hook_test_root()),
    }
