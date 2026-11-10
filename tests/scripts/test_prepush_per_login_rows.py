# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A second login on a capacity host resolves to its own uv and run folder (OMN-23355).

The host row carries the first operator's uv and run folder. The per-login overlay
(``prepush_logins``, found through the skill overlay roots) carries one row per
(label, login); the reader replaces the two columns for the invoking login only.
The ssh target stays a host. The logins here are invented."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.hooks import pytest_full_suite_host_guard as guard
from tests.scripts._prepush_private_table import (
    HOST_E,
    SYNTHETIC_TABLE,
    write_private_root,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRST = "first-login"
SECOND = "second-login"
SECOND_UV = "/opt/second-login/bin/uv"
SECOND_WORKROOT = "/srv/second-login/onex-prepush"
ROW_UV = "/opt/synthetic/bin/uv"
ROW_WORKROOT = "/srv/onex-prepush"


def _overlay(base: Path, rows: str) -> Path:
    root = base / "overlays"
    target = root / guard.LOGIN_OVERLAY_NAME / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# invented rows\ntable: |\n  # label login uv_abs_path workroot\n"
        + "".join(f"  {line}\n" for line in rows.splitlines())
        + "other: 1\n",
        encoding="utf-8",
    )
    return root


def _env(base: Path, rows: str | None) -> dict[str, str]:
    env = {
        "ONEX_WORKSPACE_CONFIG_ROOT": str(write_private_root(base)),
        "PATH": os.environ.get("PATH", ""),
    }
    if rows is not None:
        env[guard.OVERLAY_ROOTS_ENV] = str(_overlay(base, rows))
    return env


def _row(table: str, label: str) -> list[str]:
    return next(
        ln.split("\t") for ln in table.splitlines() if ln.startswith(f"{label}\t")
    )


def test_per_login_prepush_second_login_resolves_to_its_own_uv_and_workroot(
    tmp_path: Path,
) -> None:
    rows = f"h051 {SECOND} {SECOND_UV} {SECOND_WORKROOT}"
    env = _env(tmp_path, rows)
    second = _row(guard._table_text(REPO_ROOT, env, login=SECOND), "h051")
    first = _row(guard._table_text(REPO_ROOT, env, login=FIRST), "h051")
    assert (second[5], second[7]) == (SECOND_UV, SECOND_WORKROOT)
    assert (first[5], first[7]) == (ROW_UV, ROW_WORKROOT)
    assert second[3] == first[3] == "192.0.2.51", "the ssh target never names a login"
    assert second[:5] + second[6:7] + second[8:] == first[:5] + first[6:7] + first[8:]


def test_per_login_prepush_other_rows_stay_on_the_host_values(tmp_path: Path) -> None:
    env = _env(tmp_path, f"h051 {SECOND} {SECOND_UV} {SECOND_WORKROOT}")
    table = guard._table_text(REPO_ROOT, env, login=SECOND)
    assert _row(table, "h201")[5] == ROW_UV
    assert _row(table, "h201")[7] == ROW_WORKROOT


def test_per_login_prepush_no_overlay_leaves_the_table_byte_identical(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, None)
    assert guard._table_text(REPO_ROOT, env, login=SECOND) == guard._table_text(
        REPO_ROOT, env, login=""
    )


def test_per_login_prepush_reader_uses_the_invoking_login_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path, f"h051 {SECOND} {SECOND_UV} {SECOND_WORKROOT}")
    monkeypatch.setattr(guard, "resolve_local_login", lambda: SECOND)
    assert _row(guard._table_text(REPO_ROOT, env), "h051")[5] == SECOND_UV
    monkeypatch.setattr(guard, "resolve_local_login", lambda: FIRST)
    assert _row(guard._table_text(REPO_ROOT, env), "h051")[5] == ROW_UV


def test_per_login_prepush_decision_line_names_the_logins_own_values(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, f"h051 {SECOND} {SECOND_UV} {SECOND_WORKROOT}")
    line = guard.login_decision(
        guard._table_text(REPO_ROOT, env, login=SECOND), host=HOST_E, login=SECOND
    )
    assert line == (
        f"prepush-guard decision: host={HOST_E} login={SECOND} row=h051 "
        f"verdict=authorized uv={SECOND_UV} workroot={SECOND_WORKROOT}"
    )
    refused = guard.login_decision(SYNTHETIC_TABLE, host="not-a-host", login=SECOND)
    assert "verdict=refused reason=not-designated" in refused


@pytest.mark.parametrize(
    "rows",
    [
        f"h051 {SECOND} relative/uv {SECOND_WORKROOT}",
        f"h051 {SECOND} {SECOND_UV}",
        f"h051 {SECOND} {SECOND_UV} {SECOND_WORKROOT}\nh051 {SECOND} /a /b",
    ],
)
def test_per_login_prepush_malformed_overlay_refuses_instead_of_reading_as_absent(
    tmp_path: Path, rows: str
) -> None:
    env = _env(tmp_path, rows)
    with pytest.raises(ValueError, match="per-login host row"):
        guard._table_text(REPO_ROOT, env, login=SECOND)
