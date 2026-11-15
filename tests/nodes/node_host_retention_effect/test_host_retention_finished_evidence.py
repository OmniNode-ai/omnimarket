# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt evidence of the lane_results terminal guard: a lane the ledger window does not hold is judged by its own receipt files.

The ledger loader answers only the rows of its window, so a TERMINAL older than the window is invisible to it. With receipt
evidence declared, such a lane still goes when its own receipts say ``final`` (``receipt_final``) or say not final but
untouched longer than ``dead_after_days`` (``receipt_dead``), and is kept as ``finished_unknown`` when they cannot say. The
ledger beats the receipts: a lane with a CLAIM newer than its TERMINAL is open, a lane token that is not a plain name never
reaches the receipt port, and an unreadable ledger holds everything. Every host, path and number below is fixture data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    LaneLedgerState,
    ReceiptReading,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    DAY,
    NOW,
    FakeClosure,
    FakeReceipts,
    entry,
    make_ports,
    results_class,
    run,
    scratch_class,
)

pytestmark = pytest.mark.unit


def kept_reasons(result: Any) -> dict[str, str]:
    return {Path(k.path).name: k.reason for k in result.kept}


def lane_root(tmp_path: Path) -> Path:
    """A lane_results root whose leaf is one of the names the contract allows."""
    root = tmp_path / "remote-lanes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def receipt_block(root: Path) -> dict[str, Any]:
    """The guard's receipt evidence: this lane_results root is also its own receipt root."""
    return {"root": str(root), "glob": "{lane}/*.json", "dead_after_days": 30}


def final_reading() -> ReceiptReading:
    return ReceiptReading(
        files=2, unreadable=0, not_final=0, newest_mtime=NOW - 20 * DAY
    )


def receipt_scene(
    tmp_path: Path,
    readings: dict[str, ReceiptReading] | None = None,
    closure: FakeClosure | None = None,
    sub: str = "",
) -> tuple[Path, Path, FakeReceipts, FakeClosure, dict[str, Any]]:
    """One old lane-omega entry with a receipt-evidence guard; the ledger window holds no lane.

    ``sub`` puts the scene in its own folder when one test builds two."""
    root = lane_root(tmp_path / sub)
    old = entry(root, "lane-omega", days=10)
    fields = {
        "age_classes": [
            results_class(
                root,
                keep_days=7,
                lane_pattern="lane-(?P<lane>[a-z]+)",
                receipt=receipt_block(root),
            )
        ]
    }
    return (
        root,
        old,
        FakeReceipts(readings),
        closure if closure is not None else FakeClosure({}),
        fields,
    )


# --------------------------------------------------------------------- outside the window


def test_old_lane_outside_the_ledger_window_goes_when_its_own_receipt_is_final(
    tmp_path: Path,
) -> None:
    _root, old, receipts, closure, fields = receipt_scene(
        tmp_path, {"omega/*.json": final_reading()}
    )
    ports = make_ports(closure=closure, receipts=receipts)

    dry = run(tmp_path, fields, ports=ports, dry_run=True)
    assert [Path(i.path).name for i in dry.items] == ["lane-omega"]
    assert old.exists()

    result = run(tmp_path, fields, ports=ports)

    assert result.status == "ok"
    assert result.errors == ()
    assert not old.exists()
    assert [i.finished_by for i in result.items] == ["receipt_final"]
    assert result.kept == ()


def test_control_the_same_lane_without_receipt_evidence_is_kept_forever(
    tmp_path: Path,
) -> None:
    # The defect the receipt block exists to fix: outside the loader window the ledger cannot say
    # the lane finished, so without receipt evidence an old, long-terminal lane is kept forever.
    root = lane_root(tmp_path)
    old = entry(root, "lane-omega", days=10)
    fields = {
        "age_classes": [
            results_class(root, keep_days=7, lane_pattern="lane-(?P<lane>[a-z]+)")
        ]
    }

    result = run(tmp_path, fields, ports=make_ports(closure=FakeClosure({})))

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "no_terminal"}
    assert result.totals[0].unknown_count == 1


def test_dead_lane_goes_after_dead_after_days(tmp_path: Path) -> None:
    dead = {
        "omega/*.json": ReceiptReading(
            files=1, unreadable=0, not_final=1, newest_mtime=NOW - 40 * DAY
        )
    }
    _root, old, receipts, closure, fields = receipt_scene(tmp_path, dead)

    result = run(tmp_path, fields, ports=make_ports(closure=closure, receipts=receipts))

    assert not old.exists()
    assert [i.finished_by for i in result.items] == ["receipt_dead"]

    not_dead_yet = {
        "omega/*.json": ReceiptReading(
            files=1, unreadable=0, not_final=1, newest_mtime=NOW - 10 * DAY
        )
    }
    _root, old, receipts, closure, fields = receipt_scene(
        tmp_path, not_dead_yet, sub="again"
    )

    result = run(tmp_path, fields, ports=make_ports(closure=closure, receipts=receipts))

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "receipt_not_final"}


def test_unreadable_or_missing_receipt_is_unknown_and_kept(tmp_path: Path) -> None:
    _root, old, receipts, closure, fields = receipt_scene(tmp_path, None)

    result = run(tmp_path, fields, ports=make_ports(closure=closure, receipts=receipts))

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "finished_unknown"}
    assert result.totals[0].unknown_count == 1

    unreadable = {
        "omega/*.json": ReceiptReading(
            files=1, unreadable=1, not_final=0, newest_mtime=NOW - 20 * DAY
        )
    }
    _root, old, receipts, closure, fields = receipt_scene(
        tmp_path, unreadable, sub="again"
    )

    result = run(tmp_path, fields, ports=make_ports(closure=closure, receipts=receipts))

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "finished_unknown"}
    assert result.totals[0].unknown_count == 1


# ---------------------------------------------------------------------------- the ledger wins


def test_lane_with_a_claim_newer_than_its_terminal_is_open_and_kept(
    tmp_path: Path,
) -> None:
    readings = {"omega/*.json": final_reading()}
    reopened = FakeClosure(
        {"omega": LaneLedgerState(claim=NOW - 1 * DAY, terminal=NOW - 9 * DAY)}
    )
    _root, old, receipts, _closure, fields = receipt_scene(tmp_path, readings, reopened)

    result = run(
        tmp_path, fields, ports=make_ports(closure=reopened, receipts=receipts)
    )

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "lane_open"}, (
        "the ledger beats the receipt"
    )

    claim_only = FakeClosure(
        {"omega": LaneLedgerState(claim=NOW - 1 * DAY, terminal=None)}
    )
    _root, old, receipts, _closure, fields = receipt_scene(
        tmp_path, readings, claim_only, sub="again"
    )

    result = run(
        tmp_path, fields, ports=make_ports(closure=claim_only, receipts=receipts)
    )

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "lane_open"}


def test_ledger_unreadable_keeps_everything_even_with_final_receipts(
    tmp_path: Path,
) -> None:
    _root, old, receipts, closure, fields = receipt_scene(
        tmp_path, {"omega/*.json": final_reading()}, closure=FakeClosure(None)
    )

    result = run(tmp_path, fields, ports=make_ports(closure=closure, receipts=receipts))

    assert result.status == "ok"
    assert result.items == ()
    assert result.errors == ("receipts-a: ledger unreadable",)
    assert old.exists()
    assert receipts.calls == [], "nothing is read while the ledger cannot be read"


# ------------------------------------------------------------------------------- the glob


def test_run_group_receipt_glob(tmp_path: Path) -> None:
    root = lane_root(tmp_path)
    old = entry(root, "rlane-omega-abc123", days=10)
    fields = {
        "age_classes": [
            results_class(
                root,
                keep_days=7,
                lane_pattern="rlane-(?P<lane>[a-z]+)-(?P<run>[0-9a-f]{6})",
                receipt={
                    "root": str(root),
                    "glob": "{lane}/rlane-{lane}-{run}.json",
                    "dead_after_days": 30,
                },
            )
        ]
    }
    pattern = "omega/rlane-omega-abc123.json"
    receipts = FakeReceipts(
        {
            pattern: ReceiptReading(
                files=1, unreadable=0, not_final=0, newest_mtime=NOW - 20 * DAY
            )
        }
    )

    result = run(
        tmp_path, fields, ports=make_ports(closure=FakeClosure({}), receipts=receipts)
    )

    assert not old.exists()
    assert [i.finished_by for i in result.items] == ["receipt_final"]
    assert receipts.calls == [(str(root), pattern)]

    escape_root = tmp_path / "escape" / "remote-lanes"
    escape_root.mkdir(parents=True)
    trick = entry(escape_root, "..-x", days=10)
    fields = {
        "age_classes": [
            results_class(
                escape_root,
                keep_days=7,
                lane_pattern="(?P<lane>.+)",
                receipt=receipt_block(escape_root),
            )
        ]
    }
    receipts = FakeReceipts()

    result = run(
        tmp_path, fields, ports=make_ports(closure=FakeClosure({}), receipts=receipts)
    )

    assert trick.exists()
    assert kept_reasons(result) == {"..-x": "finished_unknown"}
    assert result.totals[0].unknown_count == 1
    assert receipts.calls == [], "a lane token holding '..' never reaches the port"


# ------------------------------------------------------------------------------- refusals


def refused_receipt_classes(tmp_path: Path) -> list[tuple[str, dict[str, Any], str]]:
    """A receipt declaration the overlay model refuses, and a string its refusal names.

    The overlay model names the invalid field path and never the value (a document may carry private facts), so those cases
    assert the path.
    """
    root = tmp_path / "remote-lanes"
    return [
        (
            "dead_after_days not above keep_days",
            results_class(
                root,
                keep_days=7,
                lane_pattern="lane-(?P<lane>[a-z]+)",
                receipt={
                    "root": str(root),
                    "glob": "{lane}/*.json",
                    "dead_after_days": 7,
                },
            ),
            "age_classes.0",
        ),
        (
            "glob with {run} but a pattern without a run group",
            results_class(
                root,
                keep_days=7,
                lane_pattern="lane-(?P<lane>[a-z]+)",
                receipt={
                    "root": str(root),
                    "glob": "{lane}/rlane-{lane}-{run}.json",
                    "dead_after_days": 30,
                },
            ),
            "age_classes.0.terminal",
        ),
        (
            "archive on lane_scratch",
            scratch_class(tmp_path / "scratch", archive=True),
            "age_classes.0",
        ),
    ]


def test_control_the_same_declarations_valid_are_accepted(tmp_path: Path) -> None:
    lanes = lane_root(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    entry(lanes, "lane-omega", days=10)
    entry(runs, "rlane-theta-abcdef", days=10)
    entry(scratch, "job-1", days=1)
    fields = {
        "age_classes": [
            results_class(
                lanes,
                keep_days=7,
                lane_pattern="lane-(?P<lane>[a-z]+)",
                receipt=receipt_block(lanes),
            ),
            results_class(
                runs,
                keep_days=7,
                name="runs-a",
                lane_pattern="rlane-(?P<lane>[a-z]+)-(?P<run>[0-9a-f]{6})",
                receipt={
                    "root": str(runs),
                    "glob": "{lane}/rlane-{lane}-{run}.json",
                    "dead_after_days": 30,
                },
            ),
            scratch_class(scratch),
        ]
    }

    result = run(tmp_path, fields, ports=make_ports(closure=FakeClosure({})))

    assert result.status == "ok"


@pytest.mark.parametrize("case", range(3))
def test_an_unsafe_receipt_declaration_is_refused_and_nothing_is_removed(
    tmp_path: Path, case: int
) -> None:
    name, age_class, needle = refused_receipt_classes(tmp_path)[case]
    root = Path(age_class["root"])
    root.mkdir(parents=True, exist_ok=True)
    old = entry(root, "lane-omega", days=10)

    result = run(tmp_path, {"age_classes": [age_class]})

    assert result.status == "refused", name
    assert result.items == ()
    assert old.exists()
    assert any(needle in refusal for refusal in result.refusals), (
        name,
        result.refusals,
    )
