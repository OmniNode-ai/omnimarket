# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen line board fixtures are what their headers say they are (OMN-22367, lean line board plan T2-B).

The fixtures sit in a public repository, so the private names, hosts and addresses in the ledger rows are replaced
(``[scrubbed]``, ``private-repo-N``) before they are stored. The header of each fixture therefore records two digests per
row: ``original_sha256`` of the row exactly as the ledger or its archive roll holds it, and ``scrubbed_sha256`` of the
row as stored. Everywhere the tests below check the stored row against its scrubbed digest. Where the ledger itself is
on the machine (``ONEX_LEDGER_PATH``), they also check that the original digest is the digest of a line in the live
file or an archive roll: the stored row is then derived from a real row, not written by hand.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from functools import cache
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_effect.source_loader import SourceRefusedError

from .line_board_support import (
    CASES,
    VARIANTS,
    header,
    run_board,
    variant_dir,
)

ROW = re.compile(r"^(?P<file>[^:\s]+):L(?P<line>\d+) (?P<text>.*)$")
PAIRS = [(case, variant) for case in CASES for variant in VARIANTS]
IDS = [f"{case}-{variant}" for case, variant in PAIRS]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stored_rows(case: str, variant: str) -> list[str]:
    path = variant_dir(case, variant) / "ledger-rows.txt"
    if not path.exists():
        return []
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        assert match is not None, f"not a ledger reader row: {line[:80]}"
        texts.append(match.group("text"))
    return texts


@cache
def _ledger_digests() -> frozenset[str] | None:
    """Digests of every line of the live ledger and its archive rolls, or None when the ledger is not on this machine."""
    value = os.environ.get("ONEX_LEDGER_PATH", "").strip()
    if not value or not Path(value).is_file():
        return None
    live = Path(value)
    files = [live, *sorted((live.parent / "archive").glob("ROLLING_WORK_LEDGER_*.md"))]
    digests: set[str] = set()
    for file in files:
        with file.open(encoding="utf-8", errors="surrogateescape") as handle:
            digests.update(_sha(line.rstrip("\n")) for line in handle)
    return frozenset(digests)


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), PAIRS, ids=IDS)
def test_stored_rows_match_their_scrubbed_digests(case: str, variant: str) -> None:
    head = header(case, variant)
    recorded = head["ledger_rows"]
    stored = _stored_rows(case, variant)
    assert len(stored) == len(recorded)
    for text, entry in zip(stored, recorded, strict=True):
        assert _sha(text) == entry["scrubbed_sha256"], entry["ref"]
        assert text.startswith(entry["ts"]), entry["ref"]
        assert f"| {entry['kind']} |" in text, entry["ref"]


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), PAIRS, ids=IDS)
def test_every_row_was_written_by_the_observation_time(case: str, variant: str) -> None:
    head = header(case, variant)
    for entry in head["ledger_rows"]:
        assert _ts(entry["ts"]) <= _ts(head["observed_at"]), entry["ref"]


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), PAIRS, ids=IDS)
def test_original_digests_are_lines_of_the_ledger(case: str, variant: str) -> None:
    digests = _ledger_digests()
    if digests is None:
        pytest.skip("ONEX_LEDGER_PATH names no ledger on this machine")
    for entry in header(case, variant)["ledger_rows"]:
        assert entry["original_sha256"] in digests, entry["ref"]


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), PAIRS, ids=IDS)
def test_absent_sources_are_marked_and_present_ones_exist(
    case: str, variant: str
) -> None:
    directory = variant_dir(case, variant)
    head = header(case, variant)
    markers = {p.name for p in directory.rglob("*.ABSENT")}
    for source, state in head["sources"].items():
        assert state.startswith(("present", "absent")), source
    assert head["sources"]["pr_watcher"].startswith("present") == (
        (directory / "home/.claude_scratch/pr-watcher/state.json").exists()
    )
    assert head["sources"]["ledger_window"].startswith("present") == (
        (directory / "ledger-rows.txt").exists()
    )
    assert head["sources"]["running_evidence"].startswith("absent") == (
        "running-evidence.json.ABSENT" in markers
    )


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), PAIRS, ids=IDS)
def test_the_effect_reads_the_fixture_it_is_given(
    case: str, variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if (case, variant) == ("s7_all_sources_unread", "observed"):
        with pytest.raises(SourceRefusedError, match=r"state\.json"):
            run_board(case, variant, tmp_path, monkeypatch)
        return
    record = run_board(case, variant, tmp_path, monkeypatch)
    assert record["lines"]
    assert record["generated_at"].startswith(header(case, variant)["observed_at"][:16])
