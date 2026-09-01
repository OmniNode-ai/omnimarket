# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repo-root pytest configuration (OMN-17435).

A conftest.py at the repo root (next to pyproject.toml) is loaded by pytest for
EVERY invocation in this rootdir, regardless of which path is targeted -- unlike
tests/conftest.py, which only loads for collections that descend into tests/.
That property is what the heavy-suite host guard needs: it must fire for a bare
``uv run pytest tests/`` the same way it fires for the git-push path.

It matters more once a lab picker exists. A dispatched heavy run is executed by
a TRANSPLANTED copy of this repo on another machine, and that copy carries this
file. The guard below is what makes a `shadow` row in
``scripts/hooks/prepush_hosts.tsv`` mean something: without it, a non-authorizing
host would happily produce a green full-suite verdict that the picker would then
accept. It reads the same COMMITTED table the bash guard reads, so the two
cannot drift into different notions of "a designated host".

``enforce`` returns immediately on CI (``CI`` / ``GITHUB_ACTIONS`` set) and on
any narrow target, so this adds nothing to a normal run.
"""

from __future__ import annotations

# pyproject.toml's `pythonpath = ["src", "."]` puts the repo root on sys.path
# for every pytest run, which is what makes this dotted import resolve.
from scripts.hooks.pytest_full_suite_host_guard import enforce

# Single source of truth for "what the heavy run is" here: matches
# FULL_SUITE_TARGET in scripts/hooks/prepush_smart_tests.sh exactly.
_FULL_SUITE_TARGET = "tests/"


def pytest_configure(config: object) -> None:
    enforce(config, _FULL_SUITE_TARGET)
