# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ruling a brief cites stays on a lab host's mirror however old it is (OMN-26412).

The reading (h202, 2026-11-22): ``lane_brief.py constraints`` ran against the host mirror and exited 3 with
"no row reads '2026-09-29T06:34:17Z | RULING | lane=orchestrator-83 |' in the ledger or archive/". The mirror holds the
rulings of the last 30 days and the cited landing ruling was 54 days old. The tests hold the declared fix: the tick
carries each pinned ruling onto a roll beside the mirror, the place a brief's reader already looks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_dispatch_intake_effect.handlers.adapter_lane_dispatch import (
    ScriptBriefBuilder,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.handlers.handler_work_ledger_host_mirror import (
    HandlerPinnedRulingsMirror,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.models.model_pinned_rulings_mirror import (
    EnumPinnedRulingsStatus,
    ModelPinnedRuling,
    ModelPinnedRulingsRequest,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.runtime_work_ledger_host_mirror_tick import (
    PINNED_RULINGS,
)

pytestmark = pytest.mark.unit

LANDING = ModelPinnedRuling(ts="2026-09-29T06:34:17Z", lane="orchestrator-83")
LANDING_ROW = (
    "2026-09-29T06:34:17Z | RULING | lane=orchestrator-83 | ticket=OMN-17427 | kind=decision | "
    '"take the model out of the loop" | landing runs with no model in the loop'
)
OTHER_ROW = "2026-09-29T06:40:00Z | RULING | lane=orchestrator-83 | ticket=OMN-17427 | unrelated row"
NEW_ROW = {
    "row_id": "a",
    "row_ts": "2026-11-21T22:00:00Z",
    "raw_row": "2026-11-21T22:00:00Z | MSG | x",
}


def _roll(directory: Path, name: str, *rows: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")


def _brief_reader_finds(mirror_file: Path, pin: ModelPinnedRuling) -> list[str]:
    """What a brief's landing check does: the rows starting with the cited cells, in the archive/ beside the file and in the file."""
    files = [*sorted((mirror_file.parent / "archive").glob("*-split.md")), mirror_file]
    return [
        line
        for held in files
        if held.exists()
        for line in held.read_text(encoding="utf-8").splitlines()
        if line.startswith(pin.prefix)
    ]


def test_golden_a_cited_ruling_older_than_the_mirror_window_is_carried_onto_the_mirror_archive(
    tmp_path: Path,
) -> None:
    rolls = tmp_path / "home/docs/tracking/archive"
    _roll(rolls, "ledger-2026-10-12-split.md", OTHER_ROW, LANDING_ROW)
    mirror_file = tmp_path / "home/.onex_state/ledger-mirror/host-ledger-mirror.md"
    mirror_file.parent.mkdir(parents=True)
    mirror_file.write_text(
        NEW_ROW["raw_row"] + "\n"
    )  # the mirror holds only the last day
    assert _brief_reader_finds(mirror_file, LANDING) == []  # the recorded refusal
    result = HandlerPinnedRulingsMirror().handle(
        ModelPinnedRulingsRequest(
            rolls_directory=rolls,
            target_path=mirror_file.parent / "archive/pinned-rulings-split.md",
            pinned=(LANDING,),
        )
    )
    assert result.status is EnumPinnedRulingsStatus.MIRRORED
    assert result.rows_written == 1
    assert _brief_reader_finds(mirror_file, LANDING) == [LANDING_ROW]


def test_golden_the_roll_is_the_same_bytes_on_a_second_run(tmp_path: Path) -> None:
    rolls = tmp_path / "archive"
    _roll(rolls, "a-split.md", LANDING_ROW)
    target = tmp_path / "mirror/archive/pinned-rulings-split.md"
    request = ModelPinnedRulingsRequest(
        rolls_directory=rolls, target_path=target, pinned=(LANDING,)
    )
    HandlerPinnedRulingsMirror().handle(request)
    first = (target.read_bytes(), target.stat().st_mtime_ns)
    HandlerPinnedRulingsMirror().handle(request)
    assert (target.read_bytes(), target.stat().st_mtime_ns) == first


def test_error_a_pinned_ruling_in_no_roll_is_named_and_the_others_are_kept(
    tmp_path: Path,
) -> None:
    rolls = tmp_path / "archive"
    _roll(rolls, "a-split.md", LANDING_ROW)
    gone = ModelPinnedRuling(ts="2026-01-01T00:00:00Z", lane="nobody")
    target = tmp_path / "mirror/archive/pinned-rulings-split.md"
    result = HandlerPinnedRulingsMirror().handle(
        ModelPinnedRulingsRequest(
            rolls_directory=rolls, target_path=target, pinned=(LANDING, gone)
        )
    )
    assert result.status is EnumPinnedRulingsStatus.MISSING
    assert result.missing == (gone.prefix,)
    assert gone.prefix in result.detail
    assert target.read_text() == LANDING_ROW + "\n"


def test_error_a_host_with_no_archive_directory_names_every_pinned_ruling(
    tmp_path: Path,
) -> None:
    result = HandlerPinnedRulingsMirror().handle(
        ModelPinnedRulingsRequest(
            rolls_directory=tmp_path / "absent",
            target_path=tmp_path / "mirror/archive/pinned-rulings-split.md",
            pinned=(LANDING,),
        )
    )
    assert result.status is EnumPinnedRulingsStatus.MISSING
    assert result.missing == (LANDING.prefix,)
    assert not (tmp_path / "mirror/archive/pinned-rulings-split.md").exists()


def test_golden_the_standing_landing_ruling_is_declared_pinned() -> None:
    assert (LANDING.ts, LANDING.lane) in {(p.ts, p.lane) for p in PINNED_RULINGS}


def test_golden_the_ruling_the_dispatcher_cites_is_one_the_mirror_pins() -> None:
    cited = (ScriptBriefBuilder.LANDING_RULING, ScriptBriefBuilder.LANDING_RULING_LANE)
    assert cited in {(p.ts, p.lane) for p in PINNED_RULINGS}


def _tick(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "read.py"
    answer = json.dumps(
        {
            "status": "ok",
            "error": None,
            "refusal": None,
            "rows": [NEW_ROW],
            "next_cursor": None,
            "freshness": {"newest_row_ts": NEW_ROW["row_ts"]},
        }
    )
    script.write_text(f"import sys\nsys.stdin.read()\nsys.stdout.write({answer!r})\n")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_work_ledger_host_mirror_effect.runtime_work_ledger_host_mirror_tick",
            "--omni-home",
            str(tmp_path / "home"),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": ":".join(sys.path),
            "ONEX_LEDGER_BUS_READ_COMMAND": f"{sys.executable} {script}",
        },
    )


def test_golden_the_tick_carries_the_cited_ruling_beside_the_mirror_and_receipts_it(
    tmp_path: Path,
) -> None:
    _roll(tmp_path / "home/docs/tracking/archive", "x-split.md", LANDING_ROW)
    done = _tick(tmp_path)
    assert done.returncode == 0, done.stderr
    mirror_file = tmp_path / "home/.onex_state/ledger-mirror/host-ledger-mirror.md"
    assert _brief_reader_finds(mirror_file, LANDING) == [LANDING_ROW]
    receipt = json.loads((tmp_path / "state/last-run.json").read_text())
    assert receipt["pinned_rulings"]["status"] == "mirrored"
    assert receipt["pinned_rulings"]["rows_written"] == len(PINNED_RULINGS)


def test_error_the_tick_exits_1_and_names_a_pinned_ruling_its_host_cannot_carry(
    tmp_path: Path,
) -> None:
    done = _tick(tmp_path)  # no archive rolls on this host
    assert done.returncode == 1
    assert LANDING.prefix in done.stdout
    receipt = json.loads((tmp_path / "state/last-run.json").read_text())
    assert receipt["exit_code"] == 1
    assert receipt["pinned_rulings"]["status"] == "missing"
    # the mirror itself was still written: one missing ruling never costs the host its recent rows
    assert (
        tmp_path / "home/.onex_state/ledger-mirror/host-ledger-mirror.md"
    ).read_text() == NEW_ROW["raw_row"] + "\n"
