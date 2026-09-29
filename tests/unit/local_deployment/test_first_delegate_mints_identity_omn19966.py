# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19966: the first local delegation on a fresh HOME mints the identity.

AC1: a first run on a temp HOME with no ``~/.omninode/`` succeeds and prints the
minted tenant id once on stderr. AC2: a second run on the same HOME prints no
mint line. The subprocess form is the real seam: ``_DEFAULT_EVIDENCE_DB_PATH``
is resolved from HOME at import, so only a fresh interpreter proves a fresh HOME.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.local_deployment.tenant_identity import (
    LocalTenantIdentityError,
    resolve_local_deployment_tenant_id,
    resolve_or_mint_local_deployment_tenant_id,
)

pytestmark = pytest.mark.unit

_PROBE = (
    "from omnimarket.local_deployment.tenant_identity import "
    "resolve_or_mint_local_deployment_tenant_id as r; print(r(None))"
)


def _run(home: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_first_run_on_fresh_home_mints_and_says_so_once_on_stderr(
    tmp_path: Path,
) -> None:
    assert not (tmp_path / ".omninode").exists()
    first = _run(tmp_path)
    assert first.returncode == 0, first.stderr
    minted = UUID(first.stdout.strip())
    assert first.stderr.count("minted") == 1
    assert str(minted) in first.stderr


def test_second_run_on_same_home_prints_no_mint_line(tmp_path: Path) -> None:
    first = _run(tmp_path)
    second = _run(tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == first.stdout.strip()
    assert "minted" not in second.stderr


def test_in_process_mint_reports_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "d.sqlite"
    a = resolve_or_mint_local_deployment_tenant_id(None, db_path=db)
    b = resolve_or_mint_local_deployment_tenant_id(None, db_path=db)
    err = capsys.readouterr().err
    assert a == b
    assert err.count("minted") == 1


def test_verified_upstream_tenant_does_not_mint(tmp_path: Path) -> None:
    db = tmp_path / "d.sqlite"
    assert (
        resolve_or_mint_local_deployment_tenant_id("verified-t", db_path=db)
        == "verified-t"
    )
    with pytest.raises(LocalTenantIdentityError):
        resolve_local_deployment_tenant_id(None, db_path=db)
