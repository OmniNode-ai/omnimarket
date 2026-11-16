# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The running-reading check of the runner group flag change fails when it should (OMN-24984).

The contract of the change carries one shell command that only the local Done gate runs, because it
reads the rolling ledger. This test runs that very command, taken from the contract file, against
ledgers it writes and a stand-in ``onex`` that answers the merge commit, so a change of the check
that stops it failing is caught on the hosted runner.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-24871.yaml"
MERGED = "0123456789abcdef0123456789abcdef01234567"
OLDER = "fedcba9876543210fedcba9876543210fedcba98"


def _check() -> str:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    (entry,) = [
        e for e in loaded["dod_evidence"] if e["id"] == "dod-omn24871-running-reading"
    ]
    (check,) = entry["checks"]
    value = check["check_value"]
    assert isinstance(value, str)
    return value


def _install(ts: str, sha: str, *, row_type: str = "STATUS", pr: str = "6332") -> str:
    return (
        f"{ts} | {row_type} | lane=example-lane | pr=omnimarket#{pr} | "
        f"installed=h053:{sha[:12]} | observed=the unit tree is installed"
    )


def _reading(
    ts: str,
    *,
    verdict: str = "WORKING",
    host: str = "h053",
    reason: str | None = None,
    row_type: str = "STATUS",
    src: str = "runner-group-exposure",
) -> str:
    cells = [
        f"{ts} | {row_type} | lane=runner-group-exposure | src={src} | host={host}",
        f"verdict={verdict}",
    ]
    if reason is not None:
        cells.append(f"reason={reason}")
    cells.append("calls=3")
    return " | ".join(cells)


def _run(tmp_path: Path, rows: list[str], merged: str = MERGED) -> int:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "onex"
    stub.write_text(
        f'#!/bin/sh\necho \'{{"result":{{"merge_commit_sha":"{merged}"}}}}\'\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    # the variable that points the check at the ledger is read from the check itself, so the test
    # follows the check and names no ledger variable of its own
    (ledger_variable,) = set(re.findall(r'"\$(\w*LEDGER\w*)"', _check()))
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", ledger_variable: str(ledger)}
    # the Done gate runs a check through the system shell
    done = subprocess.run(
        ["/bin/sh", "-c", _check()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


INSTALL_AT = "2026-11-17T09:00:00Z"
GOOD = [
    _install(INSTALL_AT, MERGED),
    _reading("2026-11-17T09:35:00Z"),
]


def test_a_reading_after_the_install_of_the_merged_sha_passes(tmp_path: Path) -> None:
    """The positive control: every other case below changes one thing of this ledger."""
    assert _run(tmp_path, GOOD) == 0


@pytest.mark.parametrize(
    ("name", "rows", "merged"),
    [
        pytest.param(
            "install of an older head",
            [_install(INSTALL_AT, OLDER), _reading("2026-11-17T09:35:00Z")],
            MERGED,
            id="install-of-an-older-head",
        ),
        pytest.param(
            "no merge commit yet",
            GOOD,
            "",
            id="no-merge-commit",
        ),
        pytest.param(
            "install row is a message, not a STATUS row",
            [
                _install(INSTALL_AT, MERGED, row_type="MSG"),
                _reading("2026-11-17T09:35:00Z"),
            ],
            MERGED,
            id="install-row-is-not-a-status-row",
        ),
        pytest.param(
            "install row names another pull request",
            [
                _install(INSTALL_AT, MERGED, pr="6329"),
                _reading("2026-11-17T09:35:00Z"),
            ],
            MERGED,
            id="install-row-names-another-pr",
        ),
        pytest.param(
            "reading row is not a STATUS row",
            [
                _install(INSTALL_AT, MERGED),
                _reading("2026-11-17T09:35:00Z", row_type="MSG"),
            ],
            MERGED,
            id="reading-row-is-not-a-status-row",
        ),
        pytest.param(
            "reading before the install",
            [_install(INSTALL_AT, MERGED), _reading("2026-11-17T08:35:00Z")],
            MERGED,
            id="reading-before-the-install",
        ),
        pytest.param(
            "reading at the second of the install",
            [_install(INSTALL_AT, MERGED), _reading(INSTALL_AT)],
            MERGED,
            id="reading-at-the-install",
        ),
        pytest.param(
            "newest reading still refused",
            [
                _install(INSTALL_AT, MERGED),
                _reading("2026-11-17T09:35:00Z"),
                _reading(
                    "2026-11-17T10:35:00Z",
                    verdict="UNKNOWN",
                    reason="group-read-refused",
                ),
            ],
            MERGED,
            id="newest-reading-refused",
        ),
        pytest.param(
            "newest reading unknown",
            [
                _install(INSTALL_AT, MERGED),
                _reading("2026-11-17T09:35:00Z", verdict="UNKNOWN"),
            ],
            MERGED,
            id="newest-reading-unknown",
        ),
        pytest.param(
            "reading of another host",
            [
                _install(INSTALL_AT, MERGED),
                _reading("2026-11-17T09:35:00Z", host="h201"),
            ],
            MERGED,
            id="reading-of-another-host",
        ),
        pytest.param(
            "reading of another source",
            [
                _install(INSTALL_AT, MERGED),
                _reading("2026-11-17T09:35:00Z", src="other-source"),
            ],
            MERGED,
            id="reading-of-another-source",
        ),
        pytest.param(
            "timestamp in another zone form",
            [
                _install(INSTALL_AT, MERGED),
                _reading("2026-11-17T09:35:00+00:00"),
            ],
            MERGED,
            id="timestamp-in-another-form",
        ),
    ],
)
def test_the_check_fails_when_one_thing_of_a_good_ledger_is_wrong(
    tmp_path: Path, name: str, rows: list[str], merged: str
) -> None:
    assert _run(tmp_path, rows, merged) != 0, name


def test_the_newest_install_row_decides_the_order(tmp_path: Path) -> None:
    """A reading between two installs of the merged sha is older than the newest install."""
    rows = [
        _install("2026-11-17T09:00:00Z", MERGED),
        _reading("2026-11-17T09:35:00Z"),
        _install("2026-11-17T10:00:00Z", MERGED),
    ]
    assert _run(tmp_path, rows) != 0
