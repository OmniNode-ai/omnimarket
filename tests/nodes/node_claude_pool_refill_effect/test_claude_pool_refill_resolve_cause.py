# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refill probe names why a host's pool resolved no label (OMN-25853).

A host whose ``node_claude_token_pool_effect resolve`` exits non-zero answers ``verdict=empty``. The
excerpt used to read ``LABEL=none RESOLVE rc=1`` and nothing else, so the tick's own output could not say
which input failed. This runs the real probe script on a local host against a stand-in dispatch venv whose
``resolve`` ends with the traceback recorded on a lab laptop whose credential database held a rollback
journal: the excerpt has to carry that last line.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from omnimarket.nodes.node_claude_pool_refill_effect.handlers import (
    handler_claude_pool_refill as refill_mod,
)

pytestmark = pytest.mark.unit

_RECORDED_LAST_LINE = "sqlite3.OperationalError: attempt to write a readonly database"

_FAKE_PYTHON = f"""#!/bin/bash
if [[ "$*" == *" resolve"* ]]; then
  echo 'Traceback (most recent call last):' >&2
  echo '  File "credential_read_adapter.py", line 39, in _columns' >&2
  echo '{_RECORDED_LAST_LINE}' >&2
  exit 1
fi
exit 99
"""


def _overlay(root: Path) -> refill_mod.Overlay:
    host = refill_mod.Host("h105", "unused", True)
    return refill_mod.Overlay(
        path=root / "overlay.yaml",
        folder="/shared/pool",
        project_id="00000000-0000-0000-0000-000000000000",
        environment="dev",
        identity_env_file="~/.id.env",
        reader=host,
        tokens=[refill_mod.Token("a", "K_A")],
        hosts=[host],
        venv_roots=[str(root)],
        path_prepend=[],
        model="m",
        prompt="say ok",
        timeout_s=5,
        limited_seconds=60,
        patterns={"refused": [], "expired": [], "limited": []},
    )


def test_empty_verdict_excerpt_names_the_resolver_failure(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".onex-dispatch-venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake = venv_bin / "python"
    fake.write_text(_FAKE_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    ov = _overlay(tmp_path)

    label, verdict, excerpt = refill_mod.probe(ov, ov.hosts[0])

    assert (label, verdict) == (None, "empty")
    assert "RESOLVE rc=1" in excerpt
    assert _RECORDED_LAST_LINE in excerpt
