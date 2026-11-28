# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26725: a check that runs ``uv`` behind ``env`` is routed into the hermetic environment.

The dev runtime's Done write gate refused OMN-26118 with 3 verified and 5 failed
where the lane's own reading was 8 verified. The runtime mounts every product
clone read-only. Five items ran ``env -u OMNI_HOME uv run ...`` or
``env PYTHONDONTWRITEBYTECODE=1 uv run ...``; ``_UV_INVOCATION_RE`` matched
``uv`` only at the start of a command or pipeline segment, so those commands
skipped the hermetic environment and ``uv`` tried to rebuild ``.venv`` inside the
read-only clone: ``failed to remove directory /app/omnibase_infra/.venv:
Read-only file system``. A writable lane worktree never meets that, so every lane
read PASS.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _UV_INVOCATION_RE,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        "uv run --frozen pytest tests/x.py -q",
        "cd a && uv run pytest",
        "env -u OMNI_HOME uv run --frozen pytest tests/ci/test_c12.py -q -n 0 --no-cov",
        "env PYTHONDONTWRITEBYTECODE=1 uv run --frozen python - <<'READBACK'\nx\nREADBACK",
        "env -u OMNI_HOME -u PYTHONPATH uv run pytest",
        "env -i PATH=/usr/bin FOO=bar uv run pytest",
        "FOO=bar uv run pytest",
        "true && env -u OMNI_HOME uv run pytest",
    ],
)
def test_uv_behind_env_or_assignments_is_a_uv_invocation(command: str) -> None:
    assert _UV_INVOCATION_RE.search(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/x/y",
        "env -u OMNI_HOME grep uv file",
        "echo uv run",
        "env FOO=bar ./verify.sh",
        "grep -n 'uv ' file",
    ],
)
def test_other_commands_are_not_uv_invocations(command: str) -> None:
    assert _UV_INVOCATION_RE.search(command) is None
