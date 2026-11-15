# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of node_host_retention_effect: each test fails when either side of the seam changes shape.

overlay <-> contract bounds <-> model, runner state (``docker top``), lane receipts and run directories, unit files.
"""

from __future__ import annotations

import importlib
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_host_retention_effect import runtime_host_retention
from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    CONTRACT_PATH,
    load_bounds,
    validate_declarations,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    DockerCiHostState,
    DockerContainerMounts,
    FilesystemLaneLiveness,
    FilesystemUnitFiles,
    LoaderLaneClosure,
    SqlitePreCommitStore,
    fold_closed_lanes,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionOverlay,
    ModelLedgerLoader,
    ModelRetentionBounds,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.runtime_rows_loader import (
    main as loader_main,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    LOADER_MODULE,
    loader_fields,
)
from tests.unit.nodes.node_ledger_rows_loader_effect.test_rows_loader_reader_seam import (
    query_node,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    MSG_ROW,
    SEAM_NOW,
    QueryNodePointRead,
)

pytestmark = pytest.mark.unit

NODE_DIR = CONTRACT_PATH.parent
IDLE = (
    "UID PID PPID C STIME TTY TIME CMD\n"
    "root 55032 54994 0 Nov12 ? 00:00:00 bash /opt/onex-runner-hooks/entrypoint.sh\n"
    "root 55228 55212 0 Nov12 ? 00:00:12 actions-runner/bin/Runner.Listener run\n"
)
BUSY = (
    IDLE
    + "root 2769000 55228 9 00:01 ? 00:00:03 actions-runner/bin/Runner.Worker spawnclient 123 124\n"
)


def completed(monkeypatch: pytest.MonkeyPatch, code: int, out: str) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def test_overlay_seam_the_example_overlay_loads_and_every_declaration_is_inside_the_contract_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row written ~/... expands where it loads; pin the home so the path check reads a real folder, not a host's symlinked home.
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    overlay = ModelHostRetentionOverlay.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    )
    bounds = load_bounds()

    assert {row.name for row in overlay.hosts} >= {"pool-a"}
    text = (NODE_DIR / "overlay.example.yaml").read_text()
    assert "/home/" not in text  # a home path is written as ~/ and expanded at load
    for row in overlay.hosts:
        assert validate_declarations(row, bounds) == [], row.name
        for age in row.age_classes:
            assert age.kind in bounds.root_leaf_names


def test_contract_seam_names_resolve_and_topics_agree() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    for block in (
        contract["input_model"],
        contract["output_model"],
        contract["handler"],
        contract["entrypoint"],
    ):
        module = importlib.import_module(block["module"])
        assert hasattr(module, block["name"])
    routed = contract["handler_routing"]["handlers"][0]
    assert routed["topic"] == contract["runtime_dispatch"]["command_topic"]
    assert (
        contract["terminal_event"]
        == contract["runtime_dispatch"]["terminal_events"]["success"]
    )
    for topic in contract["runtime_dispatch"]["terminal_events"].values():
        assert topic in contract["externally_consumed_topics"]
    assert ModelRetentionBounds.model_validate(contract["retention_bounds"])
    module_path, _, attribute = contract["overlay"]["model"].rpartition(".")
    assert (
        getattr(importlib.import_module(module_path), attribute)
        is ModelHostRetentionOverlay
    )
    assert (
        contract["name"]
        in yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())["entry_points"][
            "onex.nodes"
        ]
    )


def test_the_node_is_registered_in_the_package_entry_points() -> None:
    pyproject = NODE_DIR.parents[3] / "pyproject.toml"
    if not pyproject.exists():  # an installed copy carries no project file
        pytest.fail("pyproject.toml not found beside the source tree")
    assert (
        'node_host_retention_effect = "omnimarket.nodes.node_host_retention_effect"'
        in pyproject.read_text()
    )


def test_runner_state_seam_reads_a_recorded_docker_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = completed(monkeypatch, 0, IDLE)
    assert DockerCiHostState(5).is_idle("ci-runner-1") is True
    assert seen == [["docker", "top", "ci-runner-1", "-eo", "pid,args"]]
    completed(monkeypatch, 0, BUSY)
    assert DockerCiHostState(5).is_idle("ci-runner-1") is False
    completed(monkeypatch, 1, "")
    assert DockerCiHostState(5).is_idle("ci-runner-1") is None


def test_container_mounts_seam_reads_recorded_docker_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(
        [(0, "abc123\ndef456\n"), (0, "/data/a\n\n/data/b/c\nvolume-name\n")]
    )

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        code, out = next(answers)
        return subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr(subprocess, "run", fake)
    assert DockerContainerMounts(5).mount_sources() == {"/data/a", "/data/b/c"}
    completed(monkeypatch, 1, "")
    assert DockerContainerMounts(5).mount_sources() is None


def test_receipt_seam_lane_run_directories_and_receipts(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    live = runs / "rlane-demo-lane-0a1b2c.ctx"
    live.mkdir(parents=True)
    (live / "run.pid").write_text(f"{os.getpid()}\n")
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    dead = runs / "rlane-dead-lane-ffeedd.ctx"
    dead.mkdir()
    (dead / "run.pid").write_text(f"{finished.pid}\n")
    (runs / "unrelated").mkdir()
    receipts = tmp_path / "receipts"
    (receipts / "lane-x").mkdir(parents=True)
    (receipts / "lane-x" / "r.json").write_text('{"final": false}')
    (receipts / "lane-y").mkdir()
    (receipts / "lane-y" / "r.json").write_text('{"final": true}')
    (receipts / "lane-z").mkdir()
    (receipts / "lane-z" / "r.json").write_text("not json")

    lanes = FilesystemLaneLiveness().live_lanes(
        [str(runs), str(tmp_path / "missing")], [str(receipts)]
    )

    assert lanes == {"demo-lane", "lane-x"}
    assert FilesystemLaneLiveness().live_lanes([str(tmp_path / "missing")], []) == set()


@pytest.mark.skipif(os.getuid() == 0, reason="root reads any directory")
def test_receipt_seam_an_unreadable_root_is_unreadable_not_empty(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    runs.chmod(0)
    try:
        assert FilesystemLaneLiveness().live_lanes([str(runs)], []) is None
    finally:
        runs.chmod(0o700)


def test_unit_files_seam_reads_units_and_drop_ins_only(tmp_path: Path) -> None:
    (tmp_path / "a.service").write_text("ExecStart=/x/one")
    (tmp_path / "a.service.d").mkdir()
    (tmp_path / "a.service.d" / "o.conf").write_text("Environment=P=/x/two")
    (tmp_path / "notes.txt").write_text("/x/three")

    texts = FilesystemUnitFiles().unit_texts([str(tmp_path), str(tmp_path / "missing")])

    assert texts is not None
    assert sorted(texts) == ["Environment=P=/x/two", "ExecStart=/x/one"]


def test_pre_commit_seam_h101_style_mac_row_declares_the_store_the_tool_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    overlay = ModelHostRetentionOverlay.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    )
    mac = next(row for row in overlay.hosts if row.name == "pool-mac")

    assert validate_declarations(mac, load_bounds()) == []
    store = next(age for age in mac.age_classes if age.kind == "pre_commit_store")
    assert Path(store.root).name == "pre-commit"
    assert store.index_db is not None
    assert Path(store.index_db).name == "db.db"
    assert Path(store.index_db).parent == Path(store.root)
    assert [Path(cache.path).name for cache in mac.uv_caches] == ["uv"]


def test_pre_commit_seam_the_index_schema_the_port_writes_matches_the_tool(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pre-commit"
    root.mkdir()
    db = root / "db.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE repos (repo TEXT NOT NULL, ref TEXT NOT NULL, "
        "path TEXT NOT NULL, PRIMARY KEY (repo, ref))"
    )
    connection.execute(
        "INSERT INTO repos (repo, ref, path) VALUES (?, ?, ?)",
        (
            "https://github.com/pre-commit/pre-commit-hooks",
            "v5.0.0",
            str(root / "repoaged"),
        ),
    )
    connection.execute(
        "INSERT INTO repos (repo, ref, path) VALUES (?, ?, ?)",
        ("https://github.com/pre-commit/mirrors-mypy", "v4.6.0", str(root / "reponew")),
    )
    connection.execute("CREATE TABLE configs (path TEXT NOT NULL, PRIMARY KEY (path))")
    connection.execute(
        "INSERT INTO configs (path) VALUES (?)",
        (str(root / "repoaged" / ".pre-commit-config.yaml"),),
    )
    connection.commit()
    connection.close()

    assert SqlitePreCommitStore().forget(str(db), str(root / "repoaged")) == "forgotten"

    check = sqlite3.connect(db)
    try:
        assert [row[0] for row in check.execute("SELECT path FROM repos")] == [
            str(root / "reponew")
        ]
        assert [row[0] for row in check.execute("SELECT path FROM configs")] == [
            str(root / "repoaged" / ".pre-commit-config.yaml")
        ]
    finally:
        check.close()


def test_pre_commit_seam_the_contract_bounds_name_the_leaf_folders() -> None:
    bounds = load_bounds()

    assert bounds.root_leaf_names["pre_commit_store"] == ("pre-commit",)
    assert "uv" in bounds.root_leaf_names["uv_cache"]


# ------------------------------------------------------------------------------------- ledger


def epoch(stamp: str) -> float:
    """The epoch seconds of a zoned ledger stamp, read as the closure fold reads one."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


LOADER = ModelLedgerLoader.model_validate(loader_fields(window_hours=720))
LOADER_ARGV = [
    "python",
    "-m",
    LOADER_MODULE,
    "--reader",
    "host-retention",
    "--window-hours",
    "720",
]
CLOSED_ROWS = (
    "2026-10-01T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | work",
    "2026-10-02T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=done | work",
    # b TERMINALed and then re-claimed: a CLAIM newer than the TERMINAL is not closed
    "2026-10-03T00:00:00Z | TERMINAL | lane=b | ticket=OMN-2 | outcome=done | work",
    "2026-10-04T00:00:00Z | CLAIM | lane=b | ticket=OMN-2 | work",
    # c has only a CLAIM
    "2026-10-05T00:00:00Z | CLAIM | lane=c | ticket=OMN-3 | work",
    # d TERMINALed twice: the newest TERMINAL is the one that counts
    "2026-10-06T00:00:00Z | TERMINAL | lane=d | ticket=OMN-4 | outcome=done | work",
    "2026-10-08T00:00:00Z | TERMINAL | lane=d | ticket=OMN-4 | outcome=done | work",
    # rows the fold skips: no row shape, a message, a stamp without its zone
    "2026-10-09 00:00:00 TERMINAL lane=h",
    "2026-10-09T00:00:00Z | MSG | from=lane-e | to=operator | id=2026-10-09T00:00:00Z-lane-e | word",
    "2026-10-07T00:00:00 | TERMINAL | lane=g | friction=none | work",
)


def test_ledger_seam_fold_closed_lanes_from_loader_rows() -> None:
    assert fold_closed_lanes(CLOSED_ROWS) == {
        "a": epoch("2026-10-02T00:00:00Z"),
        "d": epoch("2026-10-08T00:00:00Z"),
    }
    assert fold_closed_lanes(()) == {}


def test_ledger_seam_the_adapter_runs_the_loader_command_and_reads_its_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = "\n".join(CLOSED_ROWS) + "\n"
    expected = {
        "a": epoch("2026-10-02T00:00:00Z"),
        "d": epoch("2026-10-08T00:00:00Z"),
    }
    for code in (0, 10):
        seen = completed(monkeypatch, code, out)
        assert LoaderLaneClosure().closed_lanes(LOADER) == expected, code
        assert seen == [LOADER_ARGV], "the loader argv plus its reader and window"
    for code in (69, 1, 3):
        completed(monkeypatch, code, out)
        assert LoaderLaneClosure().closed_lanes(LOADER) is None, (
            f"exit {code} carries no rows"
        )


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd="loader", timeout=5),
        FileNotFoundError("loader"),
    ],
)
def test_ledger_seam_a_loader_that_cannot_run_is_unreadable_not_empty(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(subprocess, "run", fake)

    assert LoaderLaneClosure().closed_lanes(LOADER) is None


def test_ledger_seam_row_shape_is_the_one_the_ledger_writer_grammar_parses() -> None:
    claim_row = "2026-10-01T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | work"
    terminal_row = (
        "2026-10-02T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=done | work"
    )
    claim = parse_row(claim_row)
    terminal = parse_row(terminal_row)
    assert claim is not None
    assert terminal is not None
    assert (claim.row_type, claim.token("lane")) == ("CLAIM", "a")
    assert (terminal.row_type, terminal.token("lane")) == ("TERMINAL", "a")

    assert fold_closed_lanes([claim_row, terminal_row]) == {
        terminal.token("lane"): epoch(terminal.stamp)
    }


JOIN_NOW = SEAM_NOW
JOIN_ROWS = (
    "2026-11-09T10:00:00Z | CLAIM | lane=lane-a | ticket=OMN-1 | work",
    "2026-11-09T10:05:00Z | TERMINAL | lane=lane-a | ticket=OMN-1 | outcome=done | work",
    "2026-11-09T10:10:00Z | CLAIM | lane=lane-b | ticket=OMN-2 | work",
    MSG_ROW,
)


def loader_env(tmp_path: Path, reader: str) -> dict[str, str]:
    (tmp_path / "reader.yaml").write_text(
        f"moved_readers: [{reader}]\nmax_watermark_age_seconds: 900\npage_size: 200\n"
        "max_pages: 50\nroute: {kind: inmemory}\n"
        "parity: {receipt_lane: work-ledger-parity, window_hours: 48}\n"
        "events: {journal_env: ONEX_LEDGER_READER_JOURNAL}\n"
    )
    (tmp_path / "loader.yaml").write_text(
        "file_fallback_allowed: false\njournal_env: ONEX_LEDGER_ROWS_LOADER_JOURNAL\n"
    )
    return {
        "ONEX_LEDGER_READER_OVERLAY": str(tmp_path / "reader.yaml"),
        "ONEX_LEDGER_ROWS_LOADER_OVERLAY": str(tmp_path / "loader.yaml"),
    }


def test_ledger_seam_the_closure_folds_what_the_real_loader_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The join: the loader's own CLI answers a window of rows, and the fold reads exactly those lines.

    It fails when the loader's output row shape changes or when the fold stops reading it.
    """
    code = loader_main(
        ["--reader", "host-retention", "--window-hours", "27"],
        environ=loader_env(tmp_path, "host-retention"),
        point_read=QueryNodePointRead(query_node(JOIN_ROWS)),
        clock=lambda: JOIN_NOW,
    )
    printed = capsys.readouterr().out

    assert code == 0
    assert fold_closed_lanes(printed.splitlines()) == {
        "lane-a": epoch("2026-11-09T10:05:00Z")
    }


def test_ledger_seam_control_a_reader_the_ledger_reader_does_not_name_prints_no_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = loader_main(
        ["--reader", "host-retention", "--window-hours", "27"],
        environ=loader_env(tmp_path, "some-other-reader"),
        point_read=QueryNodePointRead(query_node(JOIN_ROWS)),
        clock=lambda: JOIN_NOW,
    )

    assert code == 69, "no node answer and no fallback is unreadable, never an empty ok"
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------------------------------- overlay


def test_overlay_seam_example_terminal_guard_is_inside_the_contract_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row written ~/... expands where it loads; pin the home so the path check reads a real folder, not a host's symlinked home.
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    overlay = ModelHostRetentionOverlay.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    )
    bounds = load_bounds()
    guarded = [
        age
        for row in overlay.hosts
        for age in row.age_classes
        if age.terminal is not None
    ]
    assert guarded, "the example overlay declares at least one terminal guard"
    for age in guarded:
        assert age.kind == "lane_results"
        assert "lane" in re.compile(age.terminal.lane_pattern).groupindex
        assert bounds.terminal_loader is not None
        assert age.terminal.loader.argv[-2:] == ("-m", bounds.terminal_loader.module)
        assert (
            age.terminal.loader.window_hours <= bounds.terminal_loader.max_window_hours
        )
    for row in overlay.hosts:
        assert validate_declarations(row, bounds) == [], row.name


# ------------------------------------------------------------------------- run directory names


def test_run_directory_name_seam(tmp_path: Path) -> None:
    pattern = re.compile(r"(?:rlane-)?(?P<lane>.+)-[0-9a-f]{6}(?:\.ctx)?")
    assert pattern.fullmatch("rlane-demo-lane-0a1b2c").group("lane") == "demo-lane"
    assert pattern.fullmatch("rlane-demo-lane-0a1b2c.ctx").group("lane") == "demo-lane"
    assert (
        pattern.fullmatch("landing-L38955-26f00d.ctx").group("lane") == "landing-L38955"
    )
    assert pattern.fullmatch("lw-8b01772067ca4d4b") is None, (
        "a name that names no lane is kept"
    )

    runs = tmp_path / "runs"
    for name in ("rlane-demo-lane-0a1b2c", "rlane-demo-lane-0a1b2c.ctx"):
        live = runs / name
        live.mkdir(parents=True)
        (live / "run.pid").write_text(f"{os.getpid()}\n")

    assert FilesystemLaneLiveness().live_lanes([str(runs)], []) == {"demo-lane"}


# -------------------------------------------------------------------------------------- unit


PLIST_TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "retention"
    / "ai.omninode.host-retention.plist.template"
)


def test_unit_seam_the_mac_timer_runs_this_node_for_the_lane_results_class() -> None:
    """The unit and the node share one command line: the module, the verb, the class and the overlay roots."""
    values = {
        "__HOME__": "/h",
        "__OMNI_HOME__": "/o",
        "__OMNIMARKET_DIR__": "/m",
        "__OVERLAY_ROOT__": "/r",
        "__HOST_NAME__": "pool-a",
    }
    text = PLIST_TEMPLATE.read_text()
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    assert "__" not in text.replace("__main__", ""), "every placeholder is replaced"
    document = plistlib.loads(text.encode())

    assert document["Label"] == "ai.omninode.host-retention"
    command = document["ProgramArguments"][-1]
    assert (
        "python -m omnimarket.nodes.node_host_retention_effect run --host pool-a --class lane_results"
        in command
    )
    assert document["EnvironmentVariables"]["ONEX_SKILL_OVERLAY_ROOTS"] == "/r"
    assert "--dry-run" not in command, (
        "the timer removes; a dry run is the same command with the flag"
    )
    # The command line the unit names parses with the node's own parser.
    args = runtime_host_retention._parser().parse_args(
        ["run", "--host", "pool-a", "--class", "lane_results"]
    )
    assert (args.host, args.classes, args.dry_run) == (
        "pool-a",
        ["lane_results"],
        False,
    )
