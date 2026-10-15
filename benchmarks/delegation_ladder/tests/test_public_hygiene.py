# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed benchmark tree names no internal system (OMN-20844).

This repository is public. The bundles and results under
``benchmarks/delegation_ladder`` were produced on a private lab, so every file
here is checked for what would identify it: private addresses and tailnet
names, home and volume paths, the names of private repositories and lab lanes,
the run and delegation ids of internal runs, and the lane name of a coordination
ledger row. A record is also checked as a record: it keeps no command line,
run id, delegation id or receipt path, and its host is a fixed label.

The names are spelled in fragments so this file does not spell what it
forbids. Each class has a positive control: the matcher is run over a synthetic
sample that must trip it, so a pattern that silently matches nothing fails here
rather than passing every file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from benchmarks.delegation_ladder.build_ledger_snapshot import (
    lane_names,
    rename_workspace,
)
from benchmarks.delegation_ladder.run_ladder import (
    PRIVATE_RECORD_FIELDS,
    PUBLISHED_HOST,
    publishable_text,
)

pytestmark = pytest.mark.unit

TREE = Path(__file__).resolve().parent.parent
SUFFIXES = {".json", ".jsonl", ".txt", ".py", ".md", ".yaml", ".yml"}

# Names that identify the private workspace and its lab, in fragments. The
# registry and worktree directory names are lower-case only: the upper-case form
# is an environment variable the driver reads, which names no directory.
_REGISTRY_NAMES = ("omni" + "_home", "omni" + "_worktrees")
_PRIVATE_NAMES = (
    "knowledge" + r"[_-]base[_-]internal",
    "kb" + r"[_-]internal",
    "omnibase" + r"[_-]internal",
    "omninode" + r"[_-]infra",
    "omni" + "web",
    "onex" + "-lab",
    "onex" + "-dev",
    "stability" + "-test",
)

PATTERNS: dict[str, re.Pattern[str]] = {
    "private address": re.compile(
        r"\b192\.168\.\d{1,3}\.\d{1,3}\b"
        r"|\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
        r"|\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"
        r"|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"
    ),
    "tailnet name": re.compile(r"\.ts\.net\b|\.tail[a-z0-9]{4,}\b"),
    "machine path": re.compile(
        r"(?-i:/Users/)[a-z0-9_.-]+|(?-i:/Vol" + r"umes/)|(?-i:/home/)[a-z0-9_.-]+/",
        re.IGNORECASE,
    ),
    "private name": re.compile(
        r"(?<![a-z0-9])(?:(?-i:"
        + "|".join(_REGISTRY_NAMES)
        + ")|"
        + "|".join(_PRIVATE_NAMES)
        + ")",
        re.IGNORECASE,
    ),
    "lab host octet": re.compile(r"(?<![\w.])\.20[0-2]\b"),
}
UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
HOST_LABEL = re.compile(r"h\d{3}|\d{1,3}(?:\.\d{1,3}){3}")
LANE_VALUE = re.compile(r"\blane=([A-Za-z0-9_.-]+)")
PLACEHOLDER_LANE = re.compile(r"lane-\d\d")

# Files whose text is the lab's own output or a ledger excerpt, where an
# internal id must not survive. Mined bundles carry the source of public
# commits, which has UUIDs of its own, so they are not held to this one.
RECORDS = sorted(
    [*TREE.glob("results/**/*.jsonl"), *TREE.glob("results/*.json")]
    + [TREE / "fixtures" / "ledger_rows_snapshot.txt"]
    + [TREE / "bundles" / f"R2-0{n}.json" for n in range(1, 5)]
)


def tree_files() -> Iterator[Path]:
    for path in sorted(TREE.rglob("*")):
        if (
            path.is_file()
            and path.suffix in SUFFIXES
            and "__pycache__" not in path.parts
        ):
            yield path


def hits(text: str) -> list[str]:
    return [name for name, pattern in PATTERNS.items() if pattern.search(text)]


def locate(path: Path, patterns: dict[str, re.Pattern[str]]) -> list[str]:
    """``file:line: class`` for each offending line, never the matched text."""
    found: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in patterns.items():
            if pattern.search(line):
                found.append(f"{path.relative_to(TREE)}:{number}: {name}")
    return found


def test_the_tree_names_no_internal_system() -> None:
    offences = [line for path in tree_files() for line in locate(path, PATTERNS)]
    assert not offences, "\n".join(offences[:40])


def test_internal_run_ids_are_absent_from_records_and_ledger_text() -> None:
    offences = [
        f"{path.relative_to(TREE)}:{number}: uuid"
        for path in RECORDS
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if UUID.search(line)
    ]
    assert RECORDS
    assert not offences, "\n".join(offences[:40])


def test_a_ledger_row_carries_a_placeholder_lane_not_a_session_name() -> None:
    texts = [
        (TREE / "fixtures" / "ledger_rows_snapshot.txt").read_text(encoding="utf-8")
    ] + [
        json.loads((TREE / "bundles" / f"R2-0{n}.json").read_text(encoding="utf-8"))[
            "scorer_config"
        ]["source_text"]
        for n in range(1, 5)
    ]
    values = [value for text in texts for value in LANE_VALUE.findall(text)]
    assert values
    assert [v for v in values if not PLACEHOLDER_LANE.fullmatch(v)] == []


def test_a_record_keeps_no_command_run_id_receipt_or_machine_name() -> None:
    checked = 0
    offences: list[str] = []
    for path in TREE.glob("results/**/*.jsonl"):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            record = json.loads(line)
            checked += 1
            where = f"{path.relative_to(TREE)}:{number}"
            offences += [
                f"{where}: field {key}"
                for key in PRIVATE_RECORD_FIELDS
                if key in record
            ]
            host = record.get("host")
            if isinstance(host, str) and HOST_LABEL.fullmatch(host):
                offences.append(f"{where}: host names a machine")
    assert checked > 500
    assert not offences, "\n".join(offences[:40])


# --------------------------------------------------------------------------
# Positive controls: each class trips the matcher on a synthetic sample.
# --------------------------------------------------------------------------


def _ip(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


_NET = "ts" + ".net"
_HOME = "ho" + "me"
_USERS = "Us" + "ers"
_OCTET = "." + "201"


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (f"served at {_ip(192, 168, 1, 20)}:8000", "private address"),
        (f"served at {_ip(10, 4, 0, 9)}", "private address"),
        (f"served at {_ip(172, 20, 0, 3)}", "private address"),
        (f"served at {_ip(100, 100, 1, 2)}", "private address"),
        ("box." + "ta" + f"il1a2b3c.{_NET}", "tailnet name"),
        (f"cd /{_HOME}/someone/work", "machine path"),
        (f"cd /{_USERS}/someone/work", "machine path"),
        ("a clone of omni" + "_home", "private name"),
        ("under omni" + "_worktrees/T-1", "private name"),
        ("a note in the knowledge" + "-base-internal", "private name"),
        ("a note in kb" + "-internal#396", "private name"),
        ("the omninode" + "_infra repo", "private name"),
        ("lane onex" + "-lab-dogfood", "private name"),
        ("the stability" + "-test lane", "private name"),
        (f"the host at {_ip(10, 1, 2, 3)} on :201", "private address"),
        (f"capacity on {_OCTET} is fine", "lab host octet"),
    ],
)
def test_each_class_trips_on_a_sample(sample: str, expected: str) -> None:
    assert expected in hits(sample)


@pytest.mark.parametrize(
    "sample",
    [
        "http://127.0.0.1:18080/health",
        "from omnimarket.nodes import x",
        "an address 8.8.8.8 and a path /srv/app/data",
        "version 3.201.4 of the package",
        "the public knowledge_base repository and $OMNI_HOME in the driver",
    ],
)
def test_public_text_does_not_trip(sample: str) -> None:
    assert hits(sample) == []


def test_a_uuid_and_a_session_lane_are_caught() -> None:
    assert UUID.search("run 9e5370c2-df8f-4d30-a403-cea1e19543e4 done")
    assert not UUID.search("run 9e5370c2 done")
    assert not PLACEHOLDER_LANE.fullmatch("friction-p5-claim-refusal")
    assert PLACEHOLDER_LANE.fullmatch("lane-07")


def test_the_redactor_removes_what_the_tree_test_forbids() -> None:
    row = (
        "2026-09-13T10:00:00Z | CLAIM | lane=fix-the-thing | worktree "
        "$OMNI_HOME/omni" + "_worktrees/T-1 on onex" + "-lab-dogfood, "
        f"tenant 9e5370c2-df8f-4d30-a403-cea1e19543e4, host {_OCTET}"
    )
    cleaned = rename_workspace(row, lane_names(row), ())
    assert hits(row)
    assert hits(cleaned) == []
    assert not UUID.search(cleaned)
    assert [v for v in LANE_VALUE.findall(cleaned) if v != "lane-01"] == []
    assert rename_workspace(cleaned, lane_names(cleaned), ()) == cleaned


def test_a_record_text_field_loses_paths_hosts_and_ids() -> None:
    raw = (
        "failed at $OMNI_HOME/.onex_state/runs/9e5370c2-df8f-4d30-a403-"
        f"cea1e19543e4/receipt.json via box.example.{_NET} {_ip(10, 1, 2, 3)} in /data/x/y"
    )
    cleaned = publishable_text(raw)
    assert hits(raw)
    assert hits(cleaned) == []
    assert not UUID.search(cleaned)
    assert "/data/" not in cleaned
    assert ".onex_state" not in cleaned


def test_the_published_host_is_a_fixed_label() -> None:
    assert not HOST_LABEL.fullmatch(PUBLISHED_HOST)
