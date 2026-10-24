# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generated archive batch headers never become part of a ledger row hash."""

import hashlib
import json

import pytest

from omnimarket.handlers.work_ledger_text import split_ledger_rows

pytestmark = pytest.mark.unit
ROWS = [
    f"2026-10-22T{hour:02d}:00:00Z | STATUS | lane=roll | req=request-{hour} | measured"
    for hour in range(4)
]
STAMP = "2026-10-23T00:00:00Z"


def _block(rows, **updates):
    metadata = {
        "source": "docs/tracking/ROLLING_WORK_LEDGER.md",
        "section_heading": "## Action Log (append-only)",
        "rolled_at": STAMP,
        "entries": len(rows),
        "first_rolled_heading": rows[0].splitlines()[0],
        "last_rolled_heading": rows[-1].splitlines()[0],
    }
    metadata.update(updates)
    header = (
        "<!-- ledger-roll-archive " + json.dumps(metadata, sort_keys=True) + " -->\n"
        f"## Rolled from ROLLING_WORK_LEDGER.md at {STAMP} -- {len(rows)} rows\n\n"
    )
    return header + "\n".join(rows) + "\n"


def test_repeated_generated_batches_preserve_exact_rows_and_hashes():
    text = _block(ROWS[:2]) + _block(ROWS[2:3]) + _block(ROWS[3:])
    parsed = split_ledger_rows(text)
    assert parsed == ROWS
    assert [hashlib.sha256(r.encode()).hexdigest() for r in parsed] == [
        hashlib.sha256(r.encode()).hexdigest() for r in ROWS
    ]


def test_continuations_around_generated_boundary_keep_their_bytes():
    first = (
        ROWS[0] + "\n  wrapped details\n## legitimate continuation heading\nmore text"
    )
    second = (
        ROWS[1] + "\n  <!-- ledger-roll-archive quoted -->\n  ## Rolled from quoted"
    )
    assert split_ledger_rows(_block([first]) + _block([second])) == [first, second]


@pytest.mark.parametrize(
    "metadata",
    [
        {"entries": 2},
        {"entries": True},
        {"entries": 0},
        {"source": "docs/tracking/OTHER.md"},
        {"source": "../ROLLING_WORK_LEDGER.md"},
        {"rolled_at": "not-a-stamp"},
        {"rolled_at": "2026-10-3T00:00:00Z"},
        {"rolled_at": "2026-10-24T00:00:00Z"},
        {"first_rolled_heading": ROWS[3]},
        {"last_rolled_heading": ROWS[3]},
        {"section_heading": "quoted prose"},
        {"unexpected": "field"},
    ],
)
def test_inconsistent_metadata_is_retained_as_continuation(metadata):
    block = _block([ROWS[1]], **metadata)
    text = ROWS[0] + "\n" + block
    parsed = split_ledger_rows(text)
    header = "\n".join(line for line in block.splitlines()[:2] if line)
    assert parsed == [ROWS[0] + "\n" + header, ROWS[1]]


@pytest.mark.parametrize(
    "alter",
    [
        lambda h: h.replace("{", "{broken", 1),
        lambda h: h.replace('"entries": 1,', '"entries": 1, "entries": 1,', 1),
        lambda h: h.replace(
            "<!-- ledger-roll-archive", "  <!-- ledger-roll-archive", 1
        ),
        lambda h: h.replace("## Rolled from", "  ## Rolled from", 1),
        lambda h: h.replace(" -- 1 rows", " -- 9 rows", 1),
        lambda h: h.replace(" -->", " --> trailing prose", 1),
        lambda h: h.replace("\n\n", "\n", 1),
        lambda h: h.replace("\n\n", "\n\nprose before the row\n", 1),
        lambda h: h.replace(
            "## Rolled from ROLLING_WORK_LEDGER.md", "## arbitrary heading", 1
        ),
    ],
)
def test_malformed_or_lookalike_headers_never_silently_drop_content(alter):
    block = alter(_block([ROWS[1]]))
    header, row = block.rsplit(ROWS[1], 1)
    parsed = split_ledger_rows(ROWS[0] + "\n" + block)
    nonblank = "\n".join(line for line in header.splitlines() if line.strip())
    assert parsed == [ROWS[0] + "\n" + nonblank, ROWS[1]]
    assert row == "\n"


def test_no_header_discards_a_row_even_when_rows_repeat():
    repeated = [ROWS[0], ROWS[0]]
    assert split_ledger_rows(_block(repeated) + _block(repeated)) == repeated * 2


def test_deeply_nested_metadata_is_retained_not_raised():
    depth = 20000
    block = _block([ROWS[1]]).replace(
        '"entries": 1', '"entries": ' + "[" * depth + "]" * depth, 1
    )
    header, _ = block.rsplit(ROWS[1], 1)
    parsed = split_ledger_rows(ROWS[0] + "\n" + block)
    nonblank = "\n".join(line for line in header.splitlines() if line.strip())
    assert parsed == [ROWS[0] + "\n" + nonblank, ROWS[1]]
