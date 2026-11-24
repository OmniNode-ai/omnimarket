# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parity check compares the file its caller names; it reads no ledger path variable and no fixed file name."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_work_ledger import parity

pytestmark = pytest.mark.unit

# The variable the check once defaulted to, spelled in two parts: this file proves it is not read.
RETIRED_VARIABLE = "ONEX_LEDGER" + "_PATH"
ROW = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | work"
SPLIT_ROW = "2026-09-27T10:00:00Z | CLAIM | lane=beta | ticket=OMN-2 | work"


def test_the_check_refuses_to_run_without_a_ledger_argument_whatever_the_environment_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = tmp_path / "ledger.md"
    named.write_text(ROW + "\n", encoding="utf-8")
    monkeypatch.setenv(RETIRED_VARIABLE, str(named))
    export = tmp_path / "projection.json"
    export.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        parity.main(
            ["--since", "2026-09-28T00:00:00Z", "--projection-json", str(export)]
        )
    assert stopped.value.code == 2


def test_archive_splits_are_found_by_the_stem_of_the_named_ledger(
    tmp_path: Path,
) -> None:
    named = tmp_path / "work_log.md"
    named.write_text(ROW + "\n", encoding="utf-8")
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "work_log_2026-09-27-split.md").write_text(
        SPLIT_ROW + "\n", encoding="utf-8"
    )
    (archive / "other_2026-09-26-split.md").write_text(
        "2026-09-26T10:00:00Z | CLAIM | lane=gamma | ticket=OMN-3 | work\n",
        encoding="utf-8",
    )
    rows = parity._read_ledger_files(named, archive)
    assert [r.split(" | ")[0] for r in rows] == [
        "2026-09-27T10:00:00Z",
        "2026-09-28T10:00:00Z",
    ]
